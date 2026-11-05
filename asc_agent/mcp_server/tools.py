"""工具实现层：每个函数就是 agent 能调用的一项能力。

约定
----
* 全部是**扁平参数**的普通函数（不是 Pydantic 包装），这样 MCP 生成的
  input_schema 是干净的 `{"properties": {...}}`，可直接喂给 OpenAI/DeepSeek
  的 function calling，不会出现嵌套 $ref。
* 全部返回 dict（MCP 会序列化成 JSON 文本），agent 侧统一按 JSON 解析。
* 全部**不抛异常给协议层**：失败也返回 `{"error": "..."}`，让模型自己纠偏。

参数校验仍走 `models.py` 的 Pydantic 模型——`validate()` 在入口处把
越界值挡掉，契约与实现共用一份定义。
"""

from __future__ import annotations

import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, ValidationError

from asc_agent import config
from asc_agent.indexing.retriever import Retriever
from asc_agent.mcp_server import code, docs, facts, lint, web
from asc_agent.mcp_server.models import (
    ApiCardRequest,
    ChipApisRequest,
    ChipFactsRequest,
    ExampleSearchRequest,
    MarkdownCheckRequest,
    ReadDocRequest,
    SearchRequest,
    SkillRequest,
    SupportRequest,
    SymbolSearchRequest,
    UTGenRequest,
)

# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_retriever() -> Retriever:
    return Retriever(config.INDEX_DB)


def _validated(model: type, **kwargs) -> tuple[dict | None, dict | None]:
    """用 Pydantic 模型校验参数：返回 (dump, error)。"""
    try:
        return model(**kwargs).model_dump(), None
    except ValidationError as exc:
        errs = [
            {"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return None, {"error": "参数校验失败", "details": errs}


def _guard(fn, **kwargs) -> dict:
    """统一兜住工具内部的意外异常，保证协议层总能看到结构化结果。"""
    try:
        return fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 - 工具层要吞掉一切，交给模型决策
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 1. 语义检索
# ---------------------------------------------------------------------------


def search_docs(
    query: Annotated[str, Field(description="自然语言问题，例如「fp32 怎么转 bf16」")],
    top_k: Annotated[int, Field(description="返回片段数，1-20", ge=1, le=20)] = 6,
    kind: Annotated[
        str, Field(description="条目类型：any/api_doc/example/skill/guide/doc")
    ] = "any",
    category: Annotated[
        str, Field(description="目录关键字过滤，如 SIMD-API/basic_api、adv_api、guide")
    ] = "",
    chip: Annotated[
        str, Field(description="芯片过滤，如 ascend910b、ascend950；仅对 API 文档生效")
    ] = "",
) -> dict:
    """检索 CANN/AscendC 资料库（API 文档、开发指南、样例、技能说明）。

    返回带出处（path + 章节 + 行号）的片段，是回答任何「怎么做/是什么/在哪」
    问题的第一步。查不到就换关键词再查一次，不要直接回答。
    """
    args, err = _validated(
        SearchRequest,
        query=query,
        top_k=top_k,
        kind=kind or "any",
        category=category or None,
        chip=chip or None,
    )
    if err:
        return err
    # 芯片名先归一成文档标签口径（`910b` → `ascend910b`）。归一失败时**不加过滤**
    # 并给出 warning：早期版本会把用户原话直接塞进 SQL，结果是「查不到一个 API 文档」
    # ——静默返回错误结果集，比报错更难排查。
    chip_tag, chip_warning = "", ""
    if args["chip"]:
        chip_tag = facts.normalize_tag(args["chip"])
        if not chip_tag:
            chip_warning = (
                f"未识别的芯片 {args['chip']!r}，已忽略芯片过滤。"
                f"可用取值：{', '.join(facts.TAG_ORDER)}（或调用 chip_facts 查规范名）"
            )
    hits = get_retriever().search(
        args["query"],
        top_k=args["top_k"],
        kind=None if args["kind"] == "any" else args["kind"],
        category=args["category"],
        chip=chip_tag or None,
    )
    out = [h.to_dict() for h in hits]
    result = {
        "query": query,
        "count": len(out),
        "hits": out,
        "hint": "" if out else "无命中：换用更短的关键词，或改用 find_symbol / find_examples。",
        "chip_filter": chip_tag or "none",
    }
    if chip_warning:
        result["warning"] = chip_warning
    return result


# ---------------------------------------------------------------------------
# 2. API 卡片
# ---------------------------------------------------------------------------


def api_card(
    name: Annotated[str, Field(description="API 名称，大小写不敏感，如 Exp、Cast、Add")],
    category: Annotated[
        str, Field(description="同名 API 消歧用，如 memory_vector_compute、reg_vector_compute")
    ] = "",
) -> dict:
    """取单个 API 的完整卡片：函数原型、参数说明、头文件、支持矩阵。

    回答「怎么用某个 API」「它在哪些芯片上支持」「该 include 什么头文件」时
    优先用它，比检索片段更结构化。
    """
    args, err = _validated(ApiCardRequest, name=name, category=category or None)
    if err:
        return err
    card = get_retriever().api_card(args["name"], args["category"])
    if not card:
        return {
            "name": name,
            "found": False,
            "hint": "apis 表与检索都没有命中。确认名称拼写，或用 search_docs 找相关主题。",
        }
    card["found"] = True
    # 指定了 category 但没命中时，retriever 会退回全部候选。这里如实告知，
    # 否则用户以为拿到的是某个实现的卡片。
    if args["category"] and args["category"].lower() not in (card.get("category") or "").lower():
        card["warning"] = (
            f"没有 category 含 {args['category']!r} 的同名文档，已返回支持矩阵最完整的一份"
            f"（{card.get('category')}）。可选目录见 alternatives。"
        )
    # 支持矩阵按芯片聚合一下，模型读起来更省 token
    grouped: dict[str, list[dict]] = {}
    for row in card.get("support", []):
        grouped.setdefault(row["chip"], []).append(
            {"prototype": row["prototype"], "product": row["product"], "supported": row["supported"]}
        )
    card["support_by_chip"] = grouped
    return card


# ---------------------------------------------------------------------------
# 3. 支持性查询
# ---------------------------------------------------------------------------


def api_support(
    api: Annotated[str, Field(description="API 名称，如 Exp")],
    chip: Annotated[str, Field(description="芯片名，留空则返回全部芯片")] = "",
) -> dict:
    """查询某个 API 在各芯片上的支持情况（合并文档标签与仓库支持性 CSV 两个来源）。

    两个来源冲突时**都要报出来**，不要自行取舍。同一 API 可能有多篇同名文档
    （regbase / membase 两套实现），每篇的支持矩阵可能不同，所以这里按文档聚合。
    """
    args, err = _validated(SupportRequest, api=api, chip=chip or None)
    if err:
        return err
    r = get_retriever()
    # 归一失败必须显式告知：早期版本会静默忽略过滤却把 chip 原样回显，
    # 用户看到 `chip: zzz` 会以为结果只针对 zzz，实际是全量。
    tag, chip_warning = "", ""
    if chip:
        tag = facts.normalize_tag(chip)
        if not tag:
            chip_warning = (
                f"未识别的芯片 {chip!r}，已按「全部芯片」返回，未做过滤。"
                f"可用取值：{', '.join(facts.TAG_ORDER)}"
            )

    by_chip = r.support_by_api(api)  # 文档标签，aggregated across 同名文档
    csv_rows = r.support(api)  # CSV 支持矩阵
    if tag:
        by_chip = {c: rows for c, rows in by_chip.items() if c == tag}
        csv_rows = [m for m in csv_rows if m["chip"] == tag]

    found = bool(by_chip or csv_rows)
    doc_paths = sorted({row["path"] for rows in by_chip.values() for row in rows})

    # 冲突判定必须**逐文档**做：同一个 API 在 membase / regbase 两套目录下各有一篇
    # 文档，支持性本来就不同；把它们聚合成「CSV=支持 且 CSV=不支持」是假冲突。
    doc_verdict: dict[str, dict[str, bool]] = {}
    for c, rows in by_chip.items():
        for row in rows:
            # 同一文档可能有多个原型分组，任一原型支持就算该文档支持
            prev = doc_verdict.setdefault(row["path"], {}).get(c)
            doc_verdict[row["path"]][c] = bool(prev) or row["supported"]

    conflicts: list[str] = []
    csv_summary: list[dict] = []
    for m in csv_rows:
        tag_chip = m["chip"]
        csv_yes = m["status"] == "yes"
        doc_flag = doc_verdict.get(m["path"], {}).get(tag_chip)
        csv_summary.append(
            {
                "chip": tag_chip,
                "status": m["status"],
                "path": m["path"],
                "grp": m["grp"],
                "doc_tag_agree": None if doc_flag is None else (doc_flag == csv_yes),
            }
        )
        if doc_flag is not None and doc_flag != csv_yes:
            conflicts.append(
                f"{tag_chip} / {m['path']}：CSV={'支持' if csv_yes else '不支持'}，"
                f"文档标签={'支持' if doc_flag else '不支持'}"
            )

    yes_tags = sorted({c for c, rows in by_chip.items() if any(x["supported"] for x in rows)})
    no_tags = sorted(
        {c for c, rows in by_chip.items() if rows and not any(x["supported"] for x in rows)}
    )
    csv_yes_tags = sorted({m["chip"] for m in csv_rows if m["status"] == "yes"})
    csv_no_tags = sorted({m["chip"] for m in csv_rows if m["status"] == "no"})

    parts: list[str] = []
    if yes_tags:
        parts.append("文档标签支持：" + "、".join(yes_tags))
    if no_tags:
        parts.append("文档标签不支持：" + "、".join(no_tags))
    if csv_yes_tags:
        parts.append("CSV 支持：" + "、".join(csv_yes_tags))
    if csv_no_tags:
        parts.append("CSV 不支持：" + "、".join(csv_no_tags))

    if not found:
        verdict = "两个来源都没有记录，按「未知」处理，不要猜。"
    else:
        verdict = "；".join(parts)
        if len(doc_paths) > 1 or len({m["path"] for m in csv_rows}) > 1:
            verdict += "。注意该 API 有多篇文档/多行 CSV 记录，支持性需按目录分别看"
        if conflicts:
            verdict += "。逐文档比对发现真冲突：" + "；".join(conflicts)

    # 按文档再切一层：同一个 API 在 regbase / membase / adv 里支持范围不同，
    # 只报「部分支持」对用户没用，必须说清是哪一份文档支持到什么程度。
    by_doc: dict[str, dict[str, dict]] = {}
    for chip_key, rows in by_chip.items():
        for row in rows:
            doc = by_doc.setdefault(row["path"], {})
            slot = doc.setdefault(chip_key, {"supported": 0, "total": 0})
            slot["total"] += 1
            slot["supported"] += 1 if row["supported"] else 0

    result = {
        "api": api,
        "chip": tag or "all",
        "chip_filter_applied": bool(tag),
        "found": found,
        "by_chip": by_chip,
        "by_doc": by_doc,
        "csv": csv_summary,
        "doc_paths": doc_paths,
        "conflicts": conflicts,
        "verdict": verdict,
    }
    if chip_warning:
        result["warning"] = chip_warning
    return result


def chip_apis(
    chip: Annotated[str, Field(description="芯片名，如 ascend910b、ascend310b")],
    group: Annotated[
        str, Field(description="API 分组前缀，如 adv_api/math_compute、c_api/reg")
    ] = "",
    only_supported: Annotated[bool, Field(description="只列支持的条目")] = True,
    limit: Annotated[int, Field(description="最多返回条数 1-500", ge=1, le=500)] = 40,
) -> dict:
    """反查某芯片支持哪些 API（走仓库自带的支持性 CSV）。"""
    args, err = _validated(
        ChipApisRequest, chip=chip, group=group or None, only_supported=only_supported, limit=limit
    )
    if err:
        return err
    tag = facts.normalize_tag(args["chip"]) or args["chip"]
    data = get_retriever().chip_api_support(tag)
    if not data["found"]:
        return {
            "chip": chip,
            "found": False,
            "known": ["ascend910b", "ascend310b"],
            "hint": "支持性 CSV 只覆盖 ascend910b / ascend310b 两代；其它芯片请用 api_support 看文档标签。",
        }
    items = data["items"]
    if args["group"]:
        items = [i for i in items if args["group"] in (i["grp"] or "")]
    if args["only_supported"]:
        items = [i for i in items if i["status"] == "yes"]
    return {
        "chip": tag,
        "group": args["group"] or "all",
        "found": True,
        "total": len(items),
        "returned": min(len(items), args["limit"]),
        "items": items[: args["limit"]],
        "overall": {"yes": data["yes"], "no": data["no"]},
    }


# ---------------------------------------------------------------------------
# 4. 芯片与 dtype 事实
# ---------------------------------------------------------------------------


def chip_facts(
    chip: Annotated[str, Field(description="芯片调用名，如 ascend910b1、ascend950pr_9599；留空列全部")] = "",
    dtype: Annotated[str, Field(description="数据类型名，如 fp8_e4m3fn_t、bfloat16_t")] = "",
) -> dict:
    """查芯片架构事实：__NPU_ARCH__、SocVersion、实现目录，以及数据类型规格。

    事实源是 asc-npu-arch 技能的 npu-arch-facts.json，**不要凭记忆回答架构号**。
    """
    args, err = _validated(ChipFactsRequest, chip=chip or None, dtype=dtype or None)
    if err:
        return err
    out: dict[str, Any] = {}
    if args["chip"]:
        out["chip"] = facts.chip_facts(args["chip"])
    if args["dtype"]:
        out["dtype"] = facts.dtype_facts(args["dtype"])
    if not out:
        out["chips"] = facts.chip_facts()
        out["dtypes"] = facts.dtype_facts()
    return out


# ---------------------------------------------------------------------------
# 5. 原文直读 / 技能读取 / 外部网页
# ---------------------------------------------------------------------------


def read_doc(
    path: Annotated[str, Field(description="相对 asc-devkit 的路径，如 docs/zh/api/.../Exp.md")],
    section: Annotated[str, Field(description="只要标题含该关键字的章节，如 约束说明")] = "",
    max_chars: Annotated[int, Field(description="正文最大字符数 200-20000", ge=200, le=20000)] = 4000,
) -> dict:
    """按路径读资料原文；给 section 可只取一章，避免一次读爆上下文。"""
    args, err = _validated(
        ReadDocRequest, path=path, section=section or None, max_chars=max_chars
    )
    if err:
        return err
    return _guard(docs.read_doc, rel_path=args["path"], section=args["section"], max_chars=args["max_chars"])


def read_skill(
    skill: Annotated[
        str, Field(description="技能名：asc-npu-arch/asc-ar-dev/asc-api-ut-gen/asc-doc-checker")
    ],
    file: Annotated[str, Field(description="技能内相对路径，默认 SKILL.md")] = "SKILL.md",
) -> dict:
    """读取 .agent/skills 下的技能说明或参考文档——这是 agent 的「行为规范」来源。"""
    args, err = _validated(SkillRequest, skill=skill, file=file or "SKILL.md")
    if err:
        return err
    return _guard(docs.read_skill, skill=args["skill"], file=args["file"])


def fetch_url(
    url: Annotated[str, Field(description="http(s) 链接，如 gitcode 上的文档或 README")],
    max_chars: Annotated[int, Field(description="返回正文最大字符数 200-20000", ge=200, le=20000)] = 6000,
) -> dict:
    """抓取在线网页并转成纯文本（自动清 HTML）。用于核对上游文档或读 issue。"""
    if not url.lower().startswith(("http://", "https://")):
        return {"url": url, "found": False, "error": "只支持 http/https 链接"}
    return _guard(web.fetch_url, url=url, max_chars=max_chars)


# ---------------------------------------------------------------------------
# 6. 文档规范检查
# ---------------------------------------------------------------------------


def check_markdown(
    path: Annotated[str, Field(description="待检查的 Markdown 相对路径")],
    max_issues: Annotated[int, Field(description="最多返回问题数 1-200", ge=1, le=200)] = 40,
) -> dict:
    """按 asc-doc-checker 的 14 条规范做机器可判定的检查（标点/标题/代码块/图注/
    芯片标签/章节顺序/术语/dtype 写法/废弃接口）。"""
    args, err = _validated(MarkdownCheckRequest, path=path, max_issues=max_issues)
    if err:
        return err
    return _guard(lint.check_markdown, path=args["path"], max_issues=args["max_issues"])


# ---------------------------------------------------------------------------
# 7. 源码侧定位
# ---------------------------------------------------------------------------


def find_symbol(
    name: Annotated[str, Field(description="符号名，如 Exp、DataCopy、SetFlag")],
    scope: Annotated[str, Field(description="搜索范围：all/include/impl/examples")] = "all",
    limit: Annotated[int, Field(description="最多命中数 1-50", ge=1, le=50)] = 10,
) -> dict:
    """在 include/impl/examples 源码里定位符号定义，补资料库检索的盲区。"""
    args, err = _validated(SymbolSearchRequest, name=name, scope=scope or "all", limit=limit)
    if err:
        return err
    return _guard(code.find_symbol, name=args["name"], scope=args["scope"], limit=args["limit"])


def find_examples(
    keyword: Annotated[str, Field(description="关键字，如 cast、matmul、double buffer")],
    limit: Annotated[int, Field(description="最多返回条数 1-30", ge=1, le=30)] = 8,
) -> dict:
    """按关键字找可运行的样例工程目录。"""
    args, err = _validated(ExampleSearchRequest, keyword=keyword, limit=limit)
    if err:
        return err
    return _guard(code.find_examples, keyword=args["keyword"], limit=args["limit"])


# ---------------------------------------------------------------------------
# 8. UT 生成（调用仓库自带脚本）
# ---------------------------------------------------------------------------

UT_SCRIPT = "asc-api-ut-gen/scripts/ut_generator_cli.py"

API_TYPE_ALIASES = {
    "membase": "aiv",   # membase 默认走 AIV 模板
    "regbase": "reg",
    "c": "c_api",
    "aiv": "aiv",
    "aic": "aic",
    "c_api": "c_api",
    "adv": "adv",
    "simt": "simt",
    "reg": "reg",
    "utils": "utils",
}


def gen_ut(
    api: Annotated[str, Field(description="API 名称，如 Add、Exp")],
    api_type: Annotated[str, Field(description="API 类型：membase/regbase/adv/c/simt/utils，或 aiv/aic")],
    chip: Annotated[str, Field(description="芯片调用名，如 ascend910b1、ascend950pr_9599")] = "ascend910b1",
    core: Annotated[str, Field(description="aic 或 aiv，可留空")] = "",
    out_dir: Annotated[str, Field(description="输出目录（相对仓库根），留空用 data/ut_out")] = "",
) -> dict:
    """调用 asc-api-ut-gen 技能里的生成器产出 gtest 骨架（只落盘，不编译、不跑）。

    生成器对「非已验证签名」的 API 会拒绝泛型生成——那时它给出的报错本身就是
    有用的结论，要原样转述给用户，并建议先读同目录 impl 与同类 UT。
    """
    args, err = _validated(
        UTGenRequest,
        api=api,
        api_type=api_type,
        chip=chip,
        core=core or None,
        out_dir=out_dir or "",
    )
    if err:
        return err
    script = config.skills_root() / UT_SCRIPT
    if not script.exists():
        return {"error": f"找不到 UT 生成器：{script}"}

    resolved_type = API_TYPE_ALIASES.get(args["api_type"], args["api_type"])
    # 生成器没有独立的 --core 参数：核心类型就是 api_type（aiv/aic）的一部分，
    # 所以 membase + core 要在这里折算成 aiv/aic。
    core = (args.get("core") or "").lower()
    if core in {"aic", "aiv"} and resolved_type in {"aiv", "aic", "membase"}:
        resolved_type = core
    out_root = Path(args["out_dir"] or "data/ut_out")
    if not out_root.is_absolute():
        out_root = config.REPO_ROOT / out_root
    out_root.mkdir(parents=True, exist_ok=True)
    # 文件名里的 api 只保留标识符字符：`api="../../x"` 否则会让 target 逃出 out_root
    safe_api = re.sub(r"[^A-Za-z0-9_]", "_", args["api"])[:64] or "api"
    filename = f"ut_{safe_api.lower()}_{resolved_type}.cpp"
    target = (out_root / filename).resolve()
    if out_root.resolve() not in target.parents:
        return {"error": f"输出路径越界：{target}"}

    cmd = [
        sys.executable, str(script),
        "--type", resolved_type,
        "--api", args["api"],
        "--chip", facts.normalize_chip(args["chip"]) or args["chip"],
        "--output", str(target),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(script.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"调用生成器失败：{exc}"}

    ok = proc.returncode == 0 and target.exists()
    body = proc.stdout.strip()[-1200:] or proc.stderr.strip()[-1200:]
    result = {
        "api": args["api"],
        "api_type": resolved_type,
        "chip": facts.normalize_chip(args["chip"]) or args["chip"],
        "ok": ok,
        "returncode": proc.returncode,
        "log": body,
    }
    if ok:
        text = target.read_text(encoding="utf-8", errors="replace")
        result["output"] = str(target)
        result["rel_path"] = str(target.relative_to(config.REPO_ROOT)).replace("\\", "/")
        result["lines"] = text.count("\n") + 1
        result["preview"] = text[:1500]
    return result


# ---------------------------------------------------------------------------
# 9. 索引状态
# ---------------------------------------------------------------------------


def repo_stats() -> dict:
    """资料库索引的规模与构建时间——判断「资料是否齐全/是否需要重建索引」。"""
    st = get_retriever().stats()
    st["skills"] = sorted(
        p.name for p in config.skills_root().iterdir() if p.is_dir()
    ) if config.skills_root().exists() else []
    return st
