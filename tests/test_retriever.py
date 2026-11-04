"""检索层测试：中文分词、排序、API 卡片、支持性聚合。

这些是 agent 的「事实来源」正确性底线，所以断言都对着具体资料路径写死的，
一旦索引或排序逻辑退化，测试会立刻红。
"""

from __future__ import annotations

import pytest

from asc_agent import config
from asc_agent.indexing.retriever import Retriever
from asc_agent.indexing.tokenizer import build_match_query, tokenize

pytestmark = pytest.mark.skipif(
    not config.INDEX_DB.exists(),
    reason="索引未构建，先跑 python -m asc_agent.indexing.indexer",
)


@pytest.fixture(scope="module")
def retriever() -> Retriever:
    r = Retriever()
    yield r
    r.close()


# ---------------------------------------------------------------- 分词


def test_tokenize_cjk_bigram():
    assert tokenize("精度转换") == "精度 度转 转换"


def test_tokenize_mixed_keeps_english_lowercase():
    tokens = tokenize("Cast 精度").split()
    assert "cast" in tokens
    assert "精度" in tokens


def test_build_match_query_wraps_cjk_as_phrase():
    q = build_match_query("精度转换")
    assert q == '"精度 度转 转换"'


def test_build_match_query_english_prefix():
    # 英文词包成 phrase 再加前缀通配：`"cast"*`
    assert build_match_query("Cast") == '"cast"*'


def test_build_match_query_empty():
    assert build_match_query("") == ""


# ---------------------------------------------------------------- 检索排序


@pytest.mark.parametrize(
    "query,expected_fragment",
    [
        ("怎么做精度转换", "precision_conversion.md"),
        ("AIV 核间怎么同步", "cpp_tensor_programming_overview.md"),
        ("入门教程", "quick_start"),
        ("错误码怎么查", "RTC_error_code.md"),
        ("Matmul 的 tiling 怎么配", "tiling_with_advanced_api.md"),
    ],
)
def test_search_hits_expected_doc(retriever: Retriever, query: str, expected_fragment: str):
    hits = retriever.search(query, top_k=3)
    assert hits, f"「{query}」没有召回任何结果"
    paths = [h.path for h in hits]
    assert any(expected_fragment in p for p in paths), (
        f"「{query}」的前 3 条里没有 {expected_fragment}，实际：{paths}"
    )


def test_search_api_name_beats_generic_words(retriever: Retriever):
    """查具体 API 名时，同名文档要排在泛泛讲「支持」「芯片」的章节前面。"""
    hits = retriever.search("Cast 怎么用", top_k=3)
    assert "type_conversion/Cast.md" in hits[0].path


def test_search_respects_kind_filter(retriever: Retriever):
    hits = retriever.search("double buffer", top_k=5, kind="guide")
    assert hits
    assert all(h.kind == "guide" for h in hits)


def test_search_chip_filter_uses_support_matrix(retriever: Retriever):
    """芯片过滤要看「该芯片被标为支持」，而不是「文档里提到过该芯片」。

    regbase 版 Abs 只在 ascend950 上支持，`chips` 列却因为参数说明里有
    `npu="950"` 之外的标签而带上别的芯片；用 doc_support 过滤才能把它在
    ascend910b 的查询里排除掉。
    """
    on_910b = retriever.search("Abs 取绝对值", top_k=5, kind="api_doc", chip="ascend910b")
    paths = [h.path for h in on_910b]
    assert paths, "910b 上应该还有 memory_vector_compute 版 Abs"
    assert not any("reg_vector_compute" in p for p in paths), paths

    on_950 = retriever.search("Abs 取绝对值", top_k=5, kind="api_doc", chip="ascend950")
    assert any("reg_vector_compute" in h.path for h in on_950), "950 应能召回 regbase 版"


def test_search_empty_query_returns_nothing(retriever: Retriever):
    assert retriever.search("   ") == []


def test_search_hits_carry_line_numbers(retriever: Retriever):
    hits = retriever.search("精度转换", top_k=1)
    assert hits[0].line >= 0
    assert hits[0].to_dict()["snippet"]


# ---------------------------------------------------------------- API 卡片


def test_api_card_returns_prototype_and_header(retriever: Retriever):
    card = retriever.api_card("Add")
    assert card is not None
    assert card["headers"], "Add 卡片应该带出头文件"
    assert "函数原型" in card["sections"] or "功能说明" in card["sections"]


def test_api_card_collects_alternatives_for_duplicate_docs(retriever: Retriever):
    """Exp 在 regbase 与 membase 各有一篇文档，卡片要提示另一篇。"""
    card = retriever.api_card("Exp")
    assert card is not None
    assert card.get("alternatives"), "Exp 有多篇同名文档，应给出 alternatives"
    assert card.get("note")


def test_api_card_falls_back_to_search_for_missing_names(retriever: Retriever):
    """DataCopy 的文件名不是 DataCopy.md，apis 表查不到，必须走检索兜底。"""
    card = retriever.api_card("DataCopy")
    assert card is not None
    assert card["path"]
    assert "DataCopy" in card["path"]


def test_api_card_unknown_returns_none(retriever: Retriever):
    assert retriever.api_card("ThisApiDefinitelyDoesNotExist") is None


# ---------------------------------------------------------------- 支持性


def test_support_matrix_keeps_prototype_granularity(retriever: Retriever):
    """Exp 的两个原型支持范围不同，必须按 prototype 分开记录。"""
    path = "docs/zh/api/SIMD-API/basic_api/memory_vector_compute/basic_arithmetic/Exp.md"
    rows = retriever.support_matrix(path)
    assert rows
    prototypes = {r["prototype"] for r in rows}
    assert len(prototypes) >= 2, f"应至少有两个原型分组，实际：{prototypes}"
    by_proto: dict[str, dict[str, bool]] = {}
    for r in rows:
        by_proto.setdefault(r["prototype"], {})[r["chip"]] = r["supported"]
    assert any(True in v.values() for v in by_proto.values())
    assert any(False in v.values() for v in by_proto.values())


def test_support_by_api_aggregates_multiple_docs(retriever: Retriever):
    by_chip = retriever.support_by_api("Exp")
    assert by_chip
    paths = {row["path"] for rows in by_chip.values() for row in rows}
    assert len(paths) >= 2, "Exp 的支持性应来自多篇文档"


def test_chip_api_support_reads_csv(retriever: Retriever):
    data = retriever.chip_api_support("ascend910b")
    assert data["found"]
    assert data["total"] > 100
    assert data["yes"] > 0


def test_csv_rows_join_docs_by_full_path(retriever: Retriever):
    """CSV 的短路径必须被补成仓库全路径，才能和 docs 精确对应。"""
    rows = retriever.support("Exp", "ascend910b")
    assert rows
    paths = [r["path"] for r in rows]
    assert all(p.startswith("docs/zh/api/SIMD-API/") for p in paths), paths
    for p in paths:
        assert (config.DEVKIT_ROOT / p).exists(), f"CSV 路径没有对应到真实文档：{p}"


# ---------------------------------------------------------------- 索引元信息


def test_stats_reports_scale(retriever: Retriever):
    st = retriever.stats()
    assert int(st["meta"]["files"]) > 3000
    assert int(st["meta"]["chunks"]) > 20000
    assert st["by_kind"].get("api_doc", 0) > 2000
