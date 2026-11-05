"""索引器测试：正常路径 + 失败要响亮（不能留下空索引）。"""

from __future__ import annotations

import sqlite3

import pytest

from asc_agent import config
from asc_agent.indexing.indexer import build_index


def test_missing_devkit_raises_before_touching_db(tmp_path):
    db = tmp_path / "idx.db"
    missing = tmp_path / "no-such-devkit"
    with pytest.raises(FileNotFoundError) as exc:
        build_index(db, missing, verbose=False)
    assert "资料库根目录不存在" in str(exc.value)
    # 报错时不该留下一个空库
    assert not db.exists()


def test_empty_devkit_raises_and_cleans_up(tmp_path):
    """一条文档都没解析到时必须报错：空索引会让之后所有查询静默「查不到」。"""
    db = tmp_path / "idx.db"
    empty = tmp_path / "empty-devkit"
    empty.mkdir()
    with pytest.raises(RuntimeError) as exc:
        build_index(db, empty, verbose=False)
    assert "没有从" in str(exc.value)
    assert not db.exists(), "失败后不能留下空索引"


def test_build_on_tiny_corpus(tmp_path):
    """构造一个最小资料库，验证索引结构与统计正确。"""
    devkit = tmp_path / "devkit"
    api = devkit / "docs" / "zh" / "api" / "SIMD-API" / "basic_api"
    api.mkdir(parents=True)
    (api / "Foo.md").write_text(
        "# Foo<a name=\"x\"></a>\n\n"
        "## 产品支持情况\n\n"
        '<!-- npu="950" id1 -->\n- Ascend 950PR/Ascend 950DT：支持\n<!-- end id1 -->\n\n'
        "## 功能说明\n\n头文件路径为：`\"basic_api/foo.h\"`。\n\n做个事情。\n\n"
        "## 函数原型\n\n```cpp\n__aicore__ inline void Foo();\n```\n",
        encoding="utf-8",
    )
    db = tmp_path / "idx.db"
    st = build_index(db, devkit, verbose=False)
    assert st.files == 1
    assert st.chunks >= 3
    assert st.apis == 1

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM fts").fetchone()[0] == st.chunks
        row = conn.execute("SELECT api_name, headers, chips FROM docs").fetchone()
        assert row[0] == "Foo"
        assert "foo.h" in (row[1] or "")
        assert "ascend950" in (row[2] or "")
        sup = conn.execute("SELECT chip, supported FROM doc_support").fetchone()
        assert sup == ("ascend950", 1)
    finally:
        conn.close()
