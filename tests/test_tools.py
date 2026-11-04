"""工具层测试：参数校验、错误兜底、事实层、文档检查、源码定位。

重点是「工具在坏输入下不能把协议打挂」——模型乱填参数是常态，
每个工具都必须返回结构化错误而不是抛异常。
"""

from __future__ import annotations

import pytest

from asc_agent import config
from asc_agent.mcp_server import code, docs, facts, lint, tools

pytestmark = pytest.mark.skipif(
    not config.INDEX_DB.exists(),
    reason="索引未构建，先跑 python -m asc_agent.indexing.indexer",
)


# ---------------------------------------------------------------- 参数校验


def test_tool_rejects_empty_query():
    out = tools.search_docs("")
    # 空 query 是合法 str，但不该炸；返回 count=0 或参数错误都算合格
    assert isinstance(out, dict)


def test_tool_rejects_out_of_range_top_k():
    out = tools.search_docs("精度转换", top_k=999)
    assert "error" in out
    assert any(d["field"] == "top_k" for d in out["details"])


def test_tool_rejects_bad_api_type():
    out = tools.gen_ut(api="Add", api_type="nonsense", chip="ascend910b1")
    assert "error" in out
    assert any(d["field"] == "api_type" for d in out["details"])


def test_read_doc_blocks_path_traversal():
    out = tools.read_doc(path="../../etc/passwd")
    assert out.get("found") is False
    assert out.get("error")


# ---------------------------------------------------------------- 检索工具


def test_search_docs_returns_hits_with_source():
    out = tools.search_docs("精度转换", top_k=3)
    assert out["count"] > 0
    hit = out["hits"][0]
    assert hit["path"] and hit["snippet"]


def test_search_docs_kind_filter():
    out = tools.search_docs("double buffer", top_k=3, kind="guide")
    assert out["count"] > 0
    assert all(h["kind"] == "guide" for h in out["hits"])


def test_search_docs_no_hit_returns_hint():
    # 无意义查询可能仍召回少量「沾边」片段（bigram 特性），关键是必须给出 hint
    out = tools.search_docs("zzzzqqqxxyy 不存在的东西")
    assert isinstance(out["count"], int)
    if out["count"] == 0:
        assert out["hint"]


# ---------------------------------------------------------------- API 卡片


def test_api_card_groups_support_by_chip():
    card = tools.api_card("Exp")
    assert card["found"] is True
    assert card["support_by_chip"]
    assert any(isinstance(v, list) for v in card["support_by_chip"].values())


def test_api_card_unknown():
    out = tools.api_card("NoSuchApiXyz")
    assert out["found"] is False
    assert out["hint"]


# ---------------------------------------------------------------- 支持性


def test_api_support_merges_two_sources():
    out = tools.api_support("Exp")
    assert out["found"] is True
    assert out["by_chip"], "应包含文档标签来源"
    assert out["csv"], "应包含 CSV 来源"
    assert out["verdict"]


def test_api_support_no_false_conflict_across_docs():
    """Exp 在 regbase/membase 两篇文档里支持性不同，但那不是「两来源冲突」。"""
    out = tools.api_support("Exp")
    assert out["conflicts"] == [], out["conflicts"]


def test_api_support_chip_filter():
    out = tools.api_support("Exp", chip="ascend910b")
    assert all(c == "ascend910b" for c in out["by_chip"])
    assert all(m["chip"] == "ascend910b" for m in out["csv"])


def test_api_support_chip_field_not_clobbered():
    """回归：by_doc 聚合曾经复用变量名 `chip`，把入参覆盖成最后一个芯片。"""
    assert tools.api_support("Exp")["chip"] == "all"
    assert tools.api_support("Exp", chip="910b")["chip"] == "910b"


def test_api_support_by_doc_breaks_down_per_document():
    out = tools.api_support("Exp")
    by_doc = out["by_doc"]
    assert len(by_doc) >= 2, "Exp 的支持性应来自多篇文档"
    for path, chips in by_doc.items():
        for chip, v in chips.items():
            assert 0 <= v["supported"] <= v["total"], (path, chip, v)


def test_api_support_unknown_api():
    out = tools.api_support("NoSuchApiXyz")
    assert out["found"] is False
    assert "未知" in out["verdict"]


def test_chip_apis_lists_supported_entries():
    out = tools.chip_apis("ascend910b", limit=10)
    assert out["found"] is True
    assert out["items"]
    assert len(out["items"]) <= 10
    assert all(i["status"] == "yes" for i in out["items"])


def test_chip_apis_unknown_chip_explains_coverage():
    """CSV 只覆盖两代芯片，问别的芯片要说明覆盖范围而不是空手而归。"""
    out = tools.chip_apis("ascend950")
    assert out["found"] is False
    assert "ascend910b" in out["known"]


# ---------------------------------------------------------------- 芯片事实


def test_chip_facts_known_chip():
    out = tools.chip_facts(chip="ascend910b1")
    chip = out["chip"]
    assert chip["found"] is True
    assert chip["npu_arch"] == "2201"
    assert chip["soc_version"] == "ASCEND910B"


def test_chip_facts_alias_normalization():
    for alias in ("910b", "A2", "910b2c", "atlasa2"):
        out = tools.chip_facts(chip=alias)
        assert out["chip"]["normalized"] == "ascend910b1", alias


def test_chip_facts_unknown_chip_lists_known():
    out = tools.chip_facts(chip="ascend12345")
    assert out["chip"]["found"] is False
    assert out["chip"]["known"]
    assert "不要臆造" in out["chip"]["note"]


def test_chip_facts_dtype_spec():
    out = tools.chip_facts(dtype="fp8_e4m3fn_t")
    spec = out["dtype"]
    assert spec["found"] is True
    assert spec["spec"]["bit_width"] == 8
    assert any(s["family"] == "ascend950" and s["supported"] for s in spec["product_support"])


def test_chip_facts_dtype_shorthand():
    out = tools.chip_facts(dtype="BF16")
    assert out["dtype"]["dtype"] == "bfloat16_t"


def test_chip_facts_lists_all_when_empty():
    out = tools.chip_facts()
    assert out["chips"]["total"] >= 6
    assert out["dtypes"]["total"] >= 20


# ---------------------------------------------------------------- 原文 / 技能


def test_read_doc_section_pick():
    path = "docs/zh/api/SIMD-API/basic_api/scalar_compute/Cast_float_to_bfloat16.md"
    out = tools.read_doc(path=path, section="约束说明")
    assert out["found"] is True
    assert out["sections"][0]["heading"].startswith("约束说明")


def test_read_doc_missing_section_lists_available():
    path = "docs/zh/api/SIMD-API/basic_api/scalar_compute/Cast_float_to_bfloat16.md"
    out = tools.read_doc(path=path, section="根本没有这一章")
    assert out["found"] is False
    assert out["sections"]


def test_read_skill_returns_skill_body():
    out = tools.read_skill(skill="asc-npu-arch")
    assert out["found"] is True
    assert "ascend910b1" in out["body"] or "统一芯片调用名" in out["body"]


def test_read_skill_unknown_lists_available():
    out = tools.read_skill(skill="no-such-skill")
    assert out["found"] is False
    assert "asc-npu-arch" in out["available_skills"]


def test_read_skill_reference_file():
    out = tools.read_skill(skill="asc-ar-dev", file="references/08_api_lookup.md")
    assert out["found"] is True
    assert len(out["body"]) > 500


# ---------------------------------------------------------------- 代码定位


def test_find_symbol_in_include():
    out = tools.find_symbol("Exp", scope="include", limit=5)
    assert out["found"] is True
    assert any(h["path"].startswith("include/") for h in out["hits"])


def test_find_symbol_marks_definitions():
    out = tools.find_symbol("Exp", scope="include", limit=10)
    assert any(h["definition"] for h in out["hits"])


def test_find_examples_returns_projects():
    out = tools.find_examples("cast", limit=5)
    assert out["found"] is True
    assert out["projects"]
    assert out["projects"][0]["project"].startswith("0")


def test_find_examples_empty_keyword():
    out = tools.find_examples("")
    assert out["found"] is False


# ---------------------------------------------------------------- UT 生成


def test_gen_ut_normalizes_api_type_aliases():
    """技能文档用 membase/regbase/c，生成器用 aiv/aic/c_api/reg，两套都要认。"""
    for api_type, expected in [
        ("membase", "aiv"),
        ("aiv", "aiv"),
        ("regbase", "reg"),
        ("reg", "reg"),
        ("c", "c_api"),
        ("c_api", "c_api"),
    ]:
        out = tools.gen_ut(api="Add", api_type=api_type, chip="ascend910b1")
        assert out.get("api_type") == expected, (api_type, out)


def test_gen_ut_success_writes_file():
    out = tools.gen_ut(api="Add", api_type="membase", chip="ascend910b1")
    assert out["ok"] is True
    assert out["lines"] > 30
    assert "gtest" in out["preview"]


def test_gen_ut_relays_regbase_arch_restriction():
    """regbase 只支持 950：工具必须原样转述生成器的约束，而不是返回假骨架。"""
    out = tools.gen_ut(api="Add", api_type="regbase", chip="ascend910b1")
    assert out["ok"] is False
    assert "regbase API only supports" in out["log"]


# ---------------------------------------------------------------- 文档检查


def _write(tmp_path, name: str, body: str):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_lint_detects_english_punct_and_spacing(tmp_path):
    p = _write(tmp_path, "docs/zh/api/x.md", "# 标题\n\n使用 API 进行调用,这里有问题。\n")
    rep = tools.check_markdown(path=str(p))
    assert rep["total_rules"] > 0
    rules = {i["rule"] for i in rep["issues"]}
    assert any("R1" in r for r in rules)


def test_lint_detects_heading_manual_number(tmp_path):
    p = _write(tmp_path, "docs/zh/api/y.md", "## 1. 安装\n\n正文。\n")
    rep = tools.check_markdown(path=str(p))
    assert any("R2" in i["rule"] for i in rep["issues"])


def test_lint_detects_code_block_without_language(tmp_path):
    p = _write(tmp_path, "docs/zh/api/z.md", "# t\n\n正文。\n\n```\ncode\n```\n")
    rep = tools.check_markdown(path=str(p))
    assert any("R3" in i["rule"] for i in rep["issues"])


def test_lint_detects_bad_chip_tag(tmp_path):
    body = (
        "# t\n\n## 产品支持情况\n\n"
        '<!-- npu="9999" id1 -->\n- Ascend 950PR/Ascend 950DT：支持\n<!-- end id1 -->\n'
    )
    p = _write(tmp_path, "docs/zh/api/tag.md", body)
    rep = tools.check_markdown(path=str(p))
    assert any("R8" in i["rule"] for i in rep["issues"])


def test_lint_detects_chip_tag_wrong_product(tmp_path):
    body = (
        "# t\n\n## 产品支持情况\n\n"
        '<!-- npu="310b" id1 -->\n- Atlas 推理系列产品：支持\n<!-- end id1 -->\n'
    )
    p = _write(tmp_path, "docs/zh/api/tag2.md", body)
    rep = tools.check_markdown(path=str(p))
    assert any("产品名" in i["message"] for i in rep["issues"])


def test_lint_detects_missing_api_section(tmp_path):
    body = "# t\n\n## 功能说明\n\n正文。\n\n## 函数原型\n\n```cpp\nvoid f();\n```\n"
    p = _write(tmp_path, "docs/zh/api/sec.md", body)
    rep = tools.check_markdown(path=str(p))
    assert any("产品支持情况" in i["message"] for i in rep["issues"])


def test_lint_detects_deprecated_api_name(tmp_path):
    p = _write(
        tmp_path,
        "docs/zh/api/dep.md",
        "# t\n\n调用 ScalarCast 完成转换，或用 ToBfloat16。\n",
    )
    rep = tools.check_markdown(path=str(p))
    deprecated = [i for i in rep["issues"] if "R14" in i["rule"]]
    assert deprecated, rep["issues"]
    assert any("Cast" in i["message"] for i in deprecated)


def test_lint_clean_doc_has_few_issues(tmp_path):
    body = (
        "# 示例接口\n\n"
        "## 产品支持情况\n\n"
        '<!-- npu="950" id1 -->\n- Ascend 950PR/Ascend 950DT：支持\n<!-- end id1 -->\n\n'
        "## 功能说明\n\n头文件路径为：`\"basic_api/x_intf.h\"`。\n\n"
        "该接口做点事情。\n\n"
        "## 函数原型\n\n```cpp\n__aicore__ inline void F();\n```\n\n"
        "## 参数说明\n\n无。\n\n"
        "## 返回值说明\n\n无。\n\n"
        "## 约束说明\n\n无。\n\n"
        "## 调用示例\n\n```cpp\nF();\n```\n"
    )
    p = _write(tmp_path, "docs/zh/api/clean.md", body)
    rep = tools.check_markdown(path=str(p))
    hard = [i for i in rep["issues"] if i["rule"].startswith(("R8", "R10", "R14"))]
    assert hard == [], hard


def test_lint_accepts_official_product_name_spacing(tmp_path):
    """`Atlas A3 训练系列产品` 里的空格是规范写法，不能报成「中英文之间加空格」。"""
    body = (
        "# t\n\n## 产品支持情况\n\n"
        '<!-- npu="A3" id1 -->\n- Atlas A3 训练系列产品/Atlas A3 推理系列产品：支持\n<!-- end id1 -->\n'
    )
    p = _write(tmp_path, "docs/zh/api/prod.md", body)
    rep = tools.check_markdown(path=str(p))
    assert not [i for i in rep["issues"] if i["rule"] == "R1 空格"]


def test_lint_ignores_tags_outside_support_section(tmp_path):
    """参数说明表里的就地 npu 标签不参与 id 序列检查（真实文档常这么写）。"""
    body = (
        "# t\n\n## 产品支持情况\n\n"
        '<!-- npu="950" id1 -->\n- Ascend 950PR/Ascend 950DT：支持\n<!-- end id1 -->\n\n'
        "## 参数说明\n\n"
        "| 参数 | 说明 |\n| --- | --- |\n"
        '| config | <!-- npu="950" id99 -->仅 950 支持<!-- end id99 --> |\n'
    )
    p = _write(tmp_path, "docs/zh/api/param.md", body)
    rep = tools.check_markdown(path=str(p))
    assert not [i for i in rep["issues"] if "标签 id" in i["rule"]]


def test_lint_reports_real_chip_order_violation():
    """真实文档 Cast_float_to_bfloat16.md 的标签顺序确实违规（310b 在 310p 前）。

    这条既验证 R8 顺序检查有效，也说明它是真阳性而不是误报。
    """
    path = "docs/zh/api/SIMD-API/basic_api/scalar_compute/Cast_float_to_bfloat16.md"
    rep = tools.check_markdown(path=path)
    order_issues = [i for i in rep["issues"] if i["rule"] == "R8 标签顺序"]
    assert order_issues, "该文档 950→A3→910b→310b→310p→910 确实不符合规定顺序"


# ---------------------------------------------------------------- 元信息


def test_repo_stats_lists_skills():
    st = tools.repo_stats()
    assert st["by_kind"]["api_doc"] > 2000
    assert "asc-npu-arch" in st["skills"]


# ---------------------------------------------------------------- 并发


def test_retriever_is_thread_safe():
    """MCP 工具跑在 anyio 工作线程池里，单例连接必须能跨线程用。

    这条是回归测试：曾经用 lru_cache 缓存连接却用默认
    check_same_thread=True，导致服务一旦并发调用就
    「SQLite objects created in a thread can only be used in that same thread」。
    """
    from concurrent.futures import ThreadPoolExecutor

    queries = ["精度转换", "Matmul", "同步机制", "DataCopy", "fp8"] * 6
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda q: tools.search_docs(q, top_k=2), queries))
    assert len(results) == len(queries)
    assert all(isinstance(r, dict) for r in results)
    assert sum(r.get("count", 0) for r in results) > 0
