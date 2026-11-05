"""构建 SQLite FTS5 索引。

索引三张表：
    docs      文档元信息（标题、分类、芯片支持、依赖头文件、API 名）
    chunks    Markdown 章节分块（正文 + bigram 分词结果）
    fts       对 chunks 的 FTS5 虚拟表（bm25 排序）
外加两张从 CSV 导入的支持性表：
    support   芯片 × API 的支持矩阵
    apis      API 卡片索引（供 api_card 工具 O(1) 命中）
"""

from __future__ import annotations

import csv
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from asc_agent import config
from asc_agent.indexing.markdown_parser import (
    api_name_from,
    chunk_section,
    classify_path,
    parse_markdown,
)
from asc_agent.indexing.tokenizer import tokenize

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS docs (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    title     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    category  TEXT NOT NULL,
    api_name  TEXT,
    chips     TEXT,
    headers   TEXT,
    mtime     REAL,
    size      INTEGER
);

CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY,
    doc_id   INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    ord      INTEGER NOT NULL,
    heading  TEXT,
    body     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

-- 独立 FTS5 表（非 external content）：行内存放 bigram 分词后的文本
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    heading, body,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS support (
    id      INTEGER PRIMARY KEY,
    chip    TEXT NOT NULL,
    grp     TEXT,
    file    TEXT NOT NULL,
    status  TEXT NOT NULL,
    only3510 TEXT,
    path    TEXT
);
CREATE INDEX IF NOT EXISTS idx_support_file ON support(file);
CREATE INDEX IF NOT EXISTS idx_support_chip ON support(chip, status);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS apis (
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    category   TEXT NOT NULL,
    path       TEXT NOT NULL,
    chips      TEXT,
    headers    TEXT,
    proto      TEXT,
    summary    TEXT,
    UNIQUE(name, path)
);
CREATE INDEX IF NOT EXISTS idx_apis_name ON apis(name);

-- 从「## 产品支持情况」里解析出的细粒度支持矩阵（保留 原型 粒度）
CREATE TABLE IF NOT EXISTS doc_support (
    doc_id     INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    chip       TEXT NOT NULL,
    prototype  TEXT,
    product    TEXT,
    supported  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_support ON doc_support(doc_id);
CREATE INDEX IF NOT EXISTS idx_doc_support_chip ON doc_support(chip, supported);
"""

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".vitepress", "figures", "images"}


@dataclass
class BuildStats:
    files: int = 0
    chunks: int = 0
    apis: int = 0
    support_rows: int = 0
    supported_docs: int = 0
    seconds: float = 0.0
    skipped: int = 0


def _iter_markdown(root: Path):
    for p in root.rglob("*.md"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        name = p.name.lower()
        if name.endswith("_en.md") or name.endswith("-en.md"):
            continue
        yield p


def _summary_of(sections) -> str:
    for s in sections:
        if "功能说明" in s.heading or "简介" in s.heading or "概述" in s.heading:
            return s.body[:400].replace("\n", " ").strip()
    if sections:
        return sections[0].body[:400].replace("\n", " ").strip()
    return ""


def build_index(
    db_path: Path | None = None,
    devkit_root: Path | None = None,
    extra_roots: list[Path] | None = None,
    verbose: bool = True,
) -> BuildStats:
    """全量重建索引（先删库文件，保证结构一致）。

    资料库不存在、或一条文档都没解析出来时**直接报错**，不留下空索引：
    空索引会让后续所有查询都「查不到」，而这个失败模式看起来完全正常
    （索引命令退出码 0、检索也不报错），排查成本极高。
    """
    db_path = db_path or config.INDEX_DB
    devkit_root = devkit_root or config.DEVKIT_ROOT
    if not Path(devkit_root).exists():
        raise FileNotFoundError(
            f"资料库根目录不存在：{devkit_root}\n"
            "请用 --devkit 指定 asc-devkit 路径，或先克隆 asc-devkit 到仓库同级。"
        )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db_path) + suffix)
        if f.exists():
            f.unlink()

    t0 = time.time()
    stats = BuildStats()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    roots: list[Path] = []
    docs_root = devkit_root / config.DOCS_SUBDIR
    if docs_root.exists():
        roots.append(docs_root)
    ex_root = devkit_root / "examples"
    if ex_root.exists():
        roots.append(ex_root)
    sk_root = devkit_root / config.SKILLS_SUBDIR
    if sk_root.exists():
        roots.append(sk_root)
    roots.extend(extra_roots or [])

    doc_rows: list[tuple] = []
    chunk_rows: list[tuple] = []  # (doc_index, ord, heading, body)
    api_rows: list[tuple] = []
    support_rows: list[tuple] = []  # (doc_index, chip, prototype, product, supported)

    seen: set[str] = set()
    for root in roots:
        for md in _iter_markdown(root):
            try:
                rel = md.relative_to(devkit_root).as_posix()
            except ValueError:
                rel = md.as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            try:
                parsed = parse_markdown(md)
            except Exception as exc:  # 单篇失败不阻塞整体
                stats.skipped += 1
                if verbose:
                    print(f"  [skip] {rel}: {exc}")
                continue
            kind, category = classify_path(rel)
            api_name = api_name_from(rel, parsed.title) if kind == "api_doc" else None
            st = md.stat()
            doc_index = len(doc_rows)
            doc_rows.append(
                (
                    rel,
                    parsed.title,
                    kind,
                    category,
                    api_name,
                    ",".join(parsed.chips),
                    ",".join(parsed.headers),
                    st.st_mtime,
                    st.st_size,
                )
            )
            stats.files += 1
            ordv = 0
            for sec in parsed.sections:
                for body in chunk_section(sec):
                    if not body.strip():
                        continue
                    chunk_rows.append((doc_index, ordv, sec.heading, body))
                    ordv += 1
                    stats.chunks += 1
            if kind == "api_doc":
                api_rows.append(
                    (
                        api_name,
                        "api",
                        category,
                        rel,
                        ",".join(parsed.chips),
                        ",".join(parsed.headers),
                        (parsed.prototypes[0][:300] if parsed.prototypes else ""),
                        _summary_of(parsed.sections),
                    )
                )
            for s in parsed.support:
                support_rows.append(
                    (doc_index, s["chip"], s.get("prototype", ""), s.get("product", ""), 1 if s["supported"] else 0)
                )
            if verbose and stats.files % 500 == 0:
                print(f"  已解析 {stats.files} 篇 / {stats.chunks} 块 ...")

    # 一条都没解析到 = 资料库布局不对（换了路径/空目录/只给了 examples 之外的目录）。
    # 此时必须报错并清掉半成品库，否则后续检索会全部「查不到」却毫无提示。
    if not doc_rows:
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(db_path) + suffix)
            if f.exists():
                f.unlink()
        raise RuntimeError(
            f"没有从 {devkit_root} 解析到任何文档。"
            "请确认该目录下有 docs/zh、examples 或 .agent/skills。"
        )

    # 写 docs，拿回 rowid
    cur = conn.executemany(
        "INSERT INTO docs(path,title,kind,category,api_name,chips,headers,mtime,size)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        doc_rows,
    )
    doc_ids = [r[0] for r in conn.execute("SELECT id FROM docs ORDER BY id")]

    conn.executemany(
        "INSERT INTO chunks(doc_id,ord,heading,body) VALUES (?,?,?,?)",
        [
            (doc_ids[d], o, h, b)
            for (d, o, h, b) in chunk_rows
        ],
    )

    # 写 FTS：正文走 bigram 分词，标题也分词（英文 API 名保持原样可精确命中）
    conn.executemany(
        "INSERT INTO fts(rowid, heading, body) VALUES (?,?,?)",
        [
            (cid, tokenize(heading), tokenize(body))
            for cid, heading, body in conn.execute(
                "SELECT id, heading, body FROM chunks"
            )
        ],
    )

    conn.executemany(
        "INSERT INTO apis(name,kind,category,path,chips,headers,proto,summary)"
        " VALUES (?,?,?,?,?,?,?,?)",
        api_rows,
    )
    stats.apis = len(api_rows)

    conn.executemany(
        "INSERT INTO doc_support(doc_id,chip,prototype,product,supported)"
        " VALUES (?,?,?,?,?)",
        [(doc_ids[d], c, p, prod, s) for (d, c, p, prod, s) in support_rows],
    )
    stats.supported_docs = len(support_rows)

    stats.support_rows = _load_support_csv(conn, devkit_root)
    conn.executemany(
        "INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)",
        [
            ("built_at", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("devkit_root", str(devkit_root)),
            ("files", str(stats.files)),
            ("chunks", str(stats.chunks)),
        ],
    )
    conn.commit()
    conn.execute("INSERT INTO fts(fts) VALUES('optimize')")
    conn.commit()
    conn.close()

    stats.seconds = round(time.time() - t0, 1)
    return stats


def _read_csv_rows(path: Path) -> list[dict]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return []
    return list(csv.DictReader(text.splitlines()))


_STATUS_NORM = {
    "支持": "yes",
    "不支持": "no",
    "无标注": "none",
    "yes": "yes",
    "no": "no",
    "none": "none",
}

# 支持性 CSV 里的 Path 是**相对 docs/zh/api/SIMD-API/** 的短路径
# （310b 用反斜杠相对路径，910b 用 docs/zh/... 全路径），统一补成仓库全路径，
# 才能与 docs 表精确 JOIN——靠 basename 兜底会有一半以上落到歧义分支。
CSV_PATH_PREFIX = "docs/zh/api/SIMD-API/"


def normalize_support_path(raw: str) -> str:
    p = (raw or "").strip().replace("\\", "/")
    if not p:
        return ""
    if p.startswith("docs/"):
        return p
    return CSV_PATH_PREFIX + p.lstrip("/")


def _load_support_csv(conn: sqlite3.Connection, devkit_root: Path) -> int:
    """把 api_*_support.csv 归一成 (chip, grp, file, status, only3510, path) 六元组。"""
    mapping = {
        "api_910b_support.csv": "ascend910b",
        "api_310b_support.csv": "ascend310b",
    }
    rows: list[tuple] = []
    for fname, chip in mapping.items():
        p = devkit_root / fname
        if not p.exists():
            continue
        for r in _read_csv_rows(p):
            status = _STATUS_NORM.get((r.get("Status") or "").strip(), "none")
            rows.append(
                (
                    chip,
                    (r.get("Group") or "").strip(),
                    (r.get("File") or "").strip(),
                    status,
                    (r.get("Only3510") or "").strip(),
                    normalize_support_path(r.get("Path") or ""),
                )
            )
    if rows:
        conn.executemany(
            "INSERT INTO support(chip,grp,file,status,only3510,path) VALUES (?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def main() -> None:  # pragma: no cover - CLI 入口
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="构建 CANN 资料助手索引")
    ap.add_argument("--db", default=str(config.INDEX_DB))
    ap.add_argument("--devkit", default=str(config.DEVKIT_ROOT))
    args = ap.parse_args()
    print(f"索引 {args.devkit} -> {args.db}")
    try:
        st = build_index(Path(args.db), Path(args.devkit))
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"索引失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(
        f"完成：{st.files} 篇文档 / {st.chunks} 块 / {st.apis} 个 API 卡片 / "
        f"{st.supported_docs} 条细粒度支持记录 / {st.support_rows} 条 CSV 记录，"
        f"用时 {st.seconds}s，跳过 {st.skipped}"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
