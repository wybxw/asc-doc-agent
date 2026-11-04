"""检索层：把用户自然语言问题变成「有证据的片段 + 出处」。

设计要点
--------
1. 两阶段：FTS5(bm25) 粗排 → Python 侧关键词覆盖度/标题命中重排。
2. 查询扩展：内置 CANN 领域同义词表（如「多核」→「多核/切分/AIV」，
   「精度转换」→「Cast/精度」），弥补纯 bigram 的语义盲区。
3. 返回结构带 path + heading + 行号估算，方便 agent 引用原文。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from asc_agent import config
from asc_agent.indexing.markdown_parser import extract_headers
from asc_agent.indexing.tokenizer import build_match_query, highlight_terms

# 问句里的功能词：不参与覆盖度打分（否则「怎么做」会把结果带偏）
STOPWORDS = {
    "怎么", "么做", "如何", "什么", "哪些", "哪个", "为什么", "是否",
    "可以", "能否", "请问", "帮我", "一下", "怎样", "么样", "介绍",
    "的是", "有没", "没有", "以及", "还有", "这个", "那个",
}

# 领域同义词/别名扩展：key 为用户可能说法，value 为文档里的说法
SYNONYMS: dict[str, list[str]] = {
    "多核": ["多核", "核间", "AIV", "block", "切分"],
    "精度": ["精度", "dtype", "数据类型", "转换"],
    "溢出": ["溢出", "饱和", "饱和模式", "overflow"],
    "内存": ["内存", "UB", "LocalTensor", "GM", "片上"],
    "搬运": ["搬运", "DataCopy", "拷贝", "copy"],
    "同步": ["同步", "SetFlag", "WaitFlag", "barrier", "SyncAll"],
    "矩阵": ["矩阵", "Matmul", "MMAD", "Cube"],
    "量化": ["量化", "quant", "反量化", "dequant"],
    "性能": ["性能", "优化", "tiling", "流水", "double buffer"],
    "fp8": ["fp8", "hifloat8", "e4m3", "e5m2", "fp8_e5m2_t"],
    "fp4": ["fp4", "fp4x2_e2m1_t", "mxfp4"],
    "int8": ["int8", "s8", "int8_t"],
    "bfloat16": ["bfloat16", "bf16", "bfloat16_t"],
    "ut": ["UT", "单元测试", "测试用例", "kernel test"],
    "报错": ["报错", "错误码", "errmsg", "ERROR", "失败"],
    "编译": ["编译", "bisheng", "ccec", "构建"],
    "芯片": ["芯片", "SocVersion", "NPU_ARCH", "产品"],
    "头文件": ["头文件", "include", "依赖", "intf.h"],
}


def _section_prior(heading: str, body: str) -> float:
    """章节先验：概念/约束/用法类章节加分，纯枚举表格降权。"""
    h = heading.lower()
    prior = 0.0
    for kw, w in (
        ("功能说明", 1.2), ("约束", 1.0), ("概述", 1.1), ("简介", 1.0),
        ("背景", 0.8), ("使用说明", 1.0), ("介绍", 0.7), ("示例", 0.5),
        ("流程", 0.8), ("分类", 0.6),
        ("参数说明", 0.2), ("返回值", 0.1), ("数据类型", 0.1),
    ):
        if kw in h:
            prior += w
            break
    # 大段表格正文（参数/枚举表）对「怎么用」类问题帮助有限
    if body.count("|") > 40 and len(body) < 900:
        prior -= 0.8
    if len(body) < 60:
        prior -= 0.6
    return prior


INTENT_RULES: list[tuple[str, str, float]] = [
    # (意图关键字, 期望命中的路径/标题特征, 加权)
    ("怎么", "overview", 2.0),
    ("怎么", "introduction", 1.6),
    ("怎么", "guide/", 1.5),
    ("如何", "overview", 2.0),
    ("如何", "guide/", 1.5),
    ("入门", "getting_started", 3.0),
    ("入门", "quick_start", 3.0),
    ("教程", "getting_started", 2.5),
    ("示例", "examples/", 2.0),
    ("样例", "examples/", 2.0),
    ("区别", "_deprecated", -2.5),
    ("区别", "overview", 1.5),
    ("支持", "/appendix/", -1.0),
    ("报错", "appendix", 2.0),
    ("错误码", "appendix", 3.0),
    ("规范", "asc_doc_", 2.0),
    ("迁移", "migration", 3.0),
    ("兼容", "migration", 2.0),
    ("兼容", "compatibility", 2.0),
    ("性能", "best_practices", 2.5),
    ("优化", "best_practices", 2.5),
]


def _intent_prior(query: str, path: str, title: str, category: str) -> float:
    """查询意图 → 文档类型的软路由。让「怎么用」优先落到概述/指南，
    「示例」优先落到 examples，「错误码」优先落到 appendix。"""
    q = query.lower()
    p = f"{path} {title} {category}".lower()
    bonus = 0.0
    for kw, need, w in INTENT_RULES:
        if kw in q and need in p:
            bonus += w
    return bonus


def _soft_negative(query: str) -> list[tuple[str, float]]:
    """查询层面的负向信号：问「区别/对比」时，废弃接口文档是干扰项。"""
    q = query.lower()
    neg: list[tuple[str, float]] = []
    if "区别" in q or "对比" in q or "差异" in q:
        neg.append(("_deprecated", -4.0))
    if "怎么" in q or "如何" in q:
        neg.append(("_deprecated", -3.0))
        neg.append(("/appendix/", -1.5))
    if "规范" not in q and "写法" not in q:
        neg.append(("asc_doc_contributing", -2.5))
        neg.append(("asc_doc_design", -2.5))
    # 「入门/教程/示例」要的是可上手的资料，文档编写规范类文件是干扰项
    if any(k in q for k in ("入门", "教程", "示例", "样例", "例子", "上手")):
        neg.append(("asc_doc_", -6.0))
        neg.append(("/appendix/", -2.0))
    return neg


# 查询里出现的「类 API 名」：CamelCase（DataCopy、SetFlag）或首字母大写的单词（Matmul、Cast）
_API_TOKEN = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*)\b")

# 这些词虽然长得像 API 名，但不是 API，命中文件名不该加权
_API_STOP = {
    "ascend", "atlas", "cann", "api", "ut", "gm", "ub", "l1", "l0a", "l0b",
    "l0c", "ai", "aic", "aiv", "cpu", "simd", "simt", "npu", "soc", "csv",
    "readme", "skill", "ok", "todo", "kernel", "core", "c", "a3", "a2",
}


def _api_tokens(query: str) -> list[str]:
    """从查询里挑出当作 API 名的 token，用来做「文件名 == API 名」的强加权。

    资料库里很多 API 的主文档文件名就是 `<API名>.md` 或 `<API名>_<场景>.md`
    （如 DataCopy_GMAndUB_continuous.md），而正文里 API 名出现次数远不如
    「芯片」「支持」这类高频词——不加这一层，「Matmul 支持哪些芯片」会被
    讲「支持」的通用章节盖过去。

    判定规则：首字母大写（Matmul/Cast/Add/DataCopy）或全大写缩写，且不在停用表里。
    中文不会被匹配，所以中文问句只靠文件名里的英文 API 名加权。
    """
    out: list[str] = []
    for m in _API_TOKEN.finditer(query):
        tok = m.group(1)
        low = tok.lower()
        if low in _API_STOP or len(tok) < 3 or tok in out:
            continue
        if not (tok[0].isupper()):
            continue
        out.append(tok)
    return out


def _file_stem_bonus(path: str, tokens: list[str]) -> float:
    """文件名（不含扩展名）与 API 名的匹配强度。"""
    if not tokens:
        return 0.0
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    low = stem.lower()
    best = 0.0
    for tok in tokens:
        t = tok.lower()
        if low == t:
            best = max(best, 9.0)
        elif low.startswith(t + "_") or low.startswith(t):
            best = max(best, 6.5)
        elif f"_{t}" in low or t in low:
            best = max(best, 2.0)
    return best


@dataclass
class Hit:
    chunk_id: int
    path: str
    title: str
    kind: str
    category: str
    heading: str
    body: str
    score: float
    chips: list[str] = field(default_factory=list)
    line: int = 0

    def to_dict(self, max_body: int = 1200) -> dict:
        body = self.body
        if len(body) > max_body:
            body = body[:max_body] + "\n…（已截断）"
        return {
            "path": self.path,
            "title": self.title,
            "heading": self.heading,
            "kind": self.kind,
            "category": self.category,
            "line": self.line,
            "score": round(self.score, 3),
            "chips": self.chips,
            "snippet": body,
        }


class Retriever:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or config.INDEX_DB)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"索引不存在：{self.db_path}\n请先运行 python -m asc_agent.indexing.indexer"
            )
        # check_same_thread=False 是必须的：MCP 的工具调用跑在 anyio 的工作线程里，
        # 而单例连接可能是在另一个线程（甚至主线程）创建的。连接本身只做只读查询，
        # 并发访问由 `self._lock` 串行化。
        self._conn = sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    # ---------- 查询扩展 ----------
    @staticmethod
    def expand(query: str) -> str:
        q = query.lower()
        extra: list[str] = []
        for key, alts in SYNONYMS.items():
            if key in q:
                extra.extend(a for a in alts if a.lower() not in q)
        return query + (" " + " ".join(extra) if extra else "")

    # ---------- 主检索 ----------
    def search(
        self,
        query: str,
        top_k: int = 8,
        kind: str | None = None,
        category: str | None = None,
        chip: str | None = None,
        expand: bool = True,
    ) -> list[Hit]:
        """两趟召回 + 统一重排。

        第一趟用「去掉问句虚词后的原查询」召回并主排序；
        第二趟用同义词扩展后的查询补召回（仅加分，不上位）。
        这样领域扩展能提召回，又不会把「怎么做」这类虚词带进排序。
        """
        if not query.strip():
            return []
        base_terms = [t for t in highlight_terms(query) if t not in STOPWORDS]
        base_q = " ".join(base_terms) or query
        exclude = _soft_negative(query)
        api_tokens = _api_tokens(query)

        rows: dict[int, sqlite3.Row] = {}
        rank_base: dict[int, int] = {}
        for i, r in enumerate(self._fetch(base_q, top_k, kind, category, chip)):
            rows[r["cid"]] = r
            rank_base[r["cid"]] = i
        if expand:
            expanded = self.expand(base_q)
            if expanded != base_q:
                for r in self._fetch(expanded, top_k, kind, category, chip):
                    rows.setdefault(r["cid"], r)

        hits: list[Hit] = []
        for cid, r in rows.items():
            body = r["body"]
            heading = r["heading"] or ""
            path = r["path"]
            # 主排序分：命中名次（第一趟）→ 归一化到 0..10
            rb = rank_base.get(cid)
            rank_score = 10.0 / (1.0 + rb) if rb is not None else 0.0
            coverage = 0.0
            if base_terms:
                hay = (heading + " " + body).lower()
                coverage = sum(1 for t in base_terms if t in hay) / len(base_terms)
            title_bonus = 0.0
            api = (r["api_name"] or "").lower()
            if api and api in query.lower():
                title_bonus += 3.0
            for tok in api_tokens:
                if api.startswith(tok.lower()):
                    title_bonus += 3.5
                    break
            if any(t in heading.lower() for t in base_terms[:4]):
                title_bonus += 1.0
            kind_bonus = {"api_doc": 0.8, "skill": 1.2, "guide": 0.6}.get(r["kind"], 0.0)
            stem_bonus = _file_stem_bonus(path, api_tokens)
            neg = sum(w for kw, w in exclude if kw in path.lower())
            final = (
                rank_score
                + coverage * 6.0
                + title_bonus
                + kind_bonus
                + stem_bonus
                + _section_prior(heading, body)
                + _intent_prior(query, path, r["title"], r["category"] or "")
                + neg
            )
            hits.append(
                Hit(
                    chunk_id=cid,
                    path=path,
                    title=r["title"],
                    kind=r["kind"],
                    category=r["category"],
                    heading=heading,
                    body=body,
                    score=final,
                    chips=(r["chips"] or "").split(",") if r["chips"] else [],
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)

        # 同一文档最多 3 块，保证结果多样
        out: list[Hit] = []
        per_doc: dict[str, int] = {}
        for h in hits:
            if per_doc.get(h.path, 0) >= 3:
                continue
            per_doc[h.path] = per_doc.get(h.path, 0) + 1
            out.append(h)
            if len(out) >= top_k:
                break
        self._fill_lines(out)
        return out

    def _fetch(
        self,
        query: str,
        top_k: int,
        kind: str | None,
        category: str | None,
        chip: str | None,
    ) -> list[sqlite3.Row]:
        match = build_match_query(query)
        if not match:
            return []
        sql = [
            "SELECT c.id AS cid, c.heading, c.body, d.path, d.title, d.kind,",
            "       d.category, d.chips, d.api_name, bm25(fts) AS bm",
            "FROM fts JOIN chunks c ON c.id = fts.rowid",
            "JOIN docs d ON d.id = c.doc_id",
            "WHERE fts MATCH ?",
        ]
        params: list = [match]
        if kind:
            sql.append("AND d.kind = ?")
            params.append(kind)
        if category:
            sql.append("AND d.category LIKE ?")
            params.append(f"%{category}%")
        if chip:
            sql.append("AND (d.chips LIKE ? OR d.kind != 'api_doc')")
            params.append(f"%{chip}%")
        sql.append("ORDER BY bm LIMIT ?")
        params.append(max(top_k * 8, 60))
        try:
            with self._lock:
                return self._conn.execute("\n".join(sql), params).fetchall()
        except sqlite3.OperationalError:
            return []

    def _fill_lines(self, hits: list[Hit]) -> None:
        """估算片段在原文中的起始行号（读文件定位首行）。"""
        cache: dict[str, str] = {}
        for h in hits:
            p = config.DEVKIT_ROOT / h.path
            if h.path not in cache:
                try:
                    cache[h.path] = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    cache[h.path] = ""
            text = cache[h.path]
            probe = h.body.strip().splitlines()[0][:40] if h.body.strip() else ""
            if probe and text:
                idx = text.find(probe)
                if idx > 0:
                    h.line = text.count("\n", 0, idx) + 1

    # ---------- API 精确卡片 ----------
    # `## 功能说明<a name="section618mcpsimp"></a>` —— 标题后面常跟 HTML 锚点，
    # 所以章节标题不能按「行尾」锚定，只能要求标题**以章节名开头**。
    def _extract_sections(self, text: str) -> dict[str, str]:
        names = ("功能说明", "函数原型", "参数说明", "返回值说明", "约束说明", "调用示例")
        out: dict[str, str] = {}
        for sec in names:
            m = re.search(
                rf"^##\s*{sec}[^\n]*$(?P<body>.*?)(?=^##\s|\Z)", text, re.M | re.S
            )
            if m and m.group("body").strip():
                out[sec] = m.group("body").strip()[:1600]
        return out

    def api_card(self, name: str, category: str | None = None) -> dict | None:
        """取 API 卡片。

        资料库里有一批 API 的文件名不是 `<API名>.md`（如 DataCopy、Matmul、
        SetFlag），`apis` 表查不到，所以最后一级兜底走 FTS 检索再从命中文档
        抽原型——宁可给出来源明确的近似卡片，也不要直接说「没有」。

        同名 API 常有多篇文档（`reg_vector_compute/Exp.md` 与
        `memory_vector_compute/Exp.md` 支持矩阵完全不同），因此这里会取出全部
        候选，按「支持芯片数 → 明细条数」挑最完整的一份，并把其余候选列在
        `alternatives` 里，提示用 category 消歧。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM apis WHERE name = ? COLLATE NOCASE ORDER BY path", (name,)
            ).fetchall()
        if category:
            rows = [r for r in rows if category.lower() in (r["category"] or "").lower()] or rows
        if not rows:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM apis WHERE name LIKE ? COLLATE NOCASE"
                    " ORDER BY LENGTH(name), path LIMIT 8",
                    (f"%{name}%",),
                ).fetchall()
        if not rows:
            return self._api_card_by_search(name)

        scored = []
        for r in rows:
            sup = self.support_matrix(r["path"])
            scored.append((sum(1 for x in sup if x["supported"]), len(sup), r, sup))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        _, _, doc_row, support = scored[0]
        doc = dict(doc_row)

        p = config.DEVKIT_ROOT / doc["path"]
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        sections = self._extract_sections(text)
        card = {
            "name": doc["name"],
            "path": doc["path"],
            "category": doc["category"],
            "chips": [c for c in (doc["chips"] or "").split(",") if c],
            "headers": [h for h in (doc["headers"] or "").split(",") if h],
            "summary": doc["summary"],
            "support": support,
            "sections": sections,
        }
        others = [
            {"path": r["path"], "category": r["category"]}
            for _, _, r, _ in scored[1:]
            if r["path"] != doc["path"]
        ]
        if others:
            card["alternatives"] = others
            card["note"] = (
                "该 API 有多篇同名文档，这里给的是支持矩阵最完整的一份；"
                "若要看其它实现形态，用 category 指定目录（见 alternatives）。"
            )
        return card

    def support_by_api(self, name: str) -> dict[str, list[dict]]:
        """按 API 名聚合**所有**同名文档的支持矩阵：`{chip: [row, ...]}`。

        为什么不能只看一张卡片：`Exp` 在 regbase 与 membase 两套文档里支持
        情况完全不同，只报一份会误导用户。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.path, s.chip, s.prototype, s.product, s.supported"
                " FROM doc_support s JOIN docs d ON d.id = s.doc_id"
                " WHERE d.api_name = ? COLLATE NOCASE ORDER BY d.path, s.chip",
                (name,),
            ).fetchall()
        by_chip: dict[str, list[dict]] = {}
        for r in rows:
            by_chip.setdefault(r["chip"], []).append(
                {
                    "path": r["path"],
                    "prototype": r["prototype"],
                    "product": r["product"],
                    "supported": bool(r["supported"]),
                }
            )
        return by_chip

    def _api_card_by_search(self, name: str) -> dict | None:
        """apis 表没有记录时的兜底：检索 → 命中 API 文档 → 抽原型/头文件/支持矩阵。"""
        hits = self.search(name, top_k=6, kind="api_doc")
        if not hits:
            return None
        hit = hits[0]
        p = config.DEVKIT_ROOT / hit.path
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        sections = self._extract_sections(text)
        return {
            "name": name,
            "path": hit.path,
            "category": hit.category,
            "chips": hit.chips,
            "headers": extract_headers(text),
            "summary": (sections.get("功能说明") or hit.body)[:400],
            "support": self.support_matrix(hit.path),
            "sections": sections,
            "note": "apis 表无同名条目，按检索命中的文档生成卡片，请以文档原文为准。",
        }

    def support_matrix(self, doc_path: str) -> list[dict]:
        """按文档取「芯片 × 原型 → 支持/不支持」明细。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.chip, s.prototype, s.product, s.supported"
                " FROM doc_support s JOIN docs d ON d.id = s.doc_id"
                " WHERE d.path = ?",
                (doc_path,),
            ).fetchall()
        return [
            {
                "chip": r["chip"],
                "prototype": r["prototype"],
                "product": r["product"],
                "supported": bool(r["supported"]),
            }
            for r in rows
        ]

    def chip_api_support(self, chip: str, api: str | None = None) -> dict:
        """某芯片支持哪些 API / 某个 API 在某芯片上是否支持（走 CSV 支持矩阵）。"""
        with_api = api is not None
        sql = (
            "SELECT grp,file,status,only3510,path FROM support"
            " WHERE chip = ?"
        )
        params: list = [chip]
        if with_api:
            sql += " AND file = ? COLLATE NOCASE"
            params.append(api)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY grp, file", params).fetchall()
        if not rows:
            return {"chip": chip, "api": api, "found": False, "items": []}
        return {
            "chip": chip,
            "api": api,
            "found": True,
            "total": len(rows),
            "yes": sum(1 for r in rows if r["status"] == "yes"),
            "no": sum(1 for r in rows if r["status"] == "no"),
            "items": [dict(r) for r in rows],
        }

    def support(self, api: str, chip: str | None = None) -> list[dict]:
        sql = "SELECT chip,grp,file,status,only3510,path FROM support WHERE file = ? COLLATE NOCASE"
        params: list = [api]
        if chip:
            sql += " AND chip = ?"
            params.append(chip)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY chip", params).fetchall()
            if not rows:
                rows = self._conn.execute(
                    "SELECT chip,grp,file,status,only3510,path FROM support"
                    " WHERE file LIKE ? COLLATE NOCASE ORDER BY chip LIMIT 20",
                    (f"%{api}%",),
                ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            meta = {r["k"]: r["v"] for r in self._conn.execute("SELECT k,v FROM meta")}
            counts = {}
            for r in self._conn.execute("SELECT kind, COUNT(*) n FROM docs GROUP BY kind"):
                counts[r["kind"]] = r["n"]
        return {"meta": meta, "by_kind": counts, "db": str(self.db_path)}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
