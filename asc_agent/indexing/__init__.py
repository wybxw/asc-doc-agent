"""把 asc-devkit 的中文 Markdown 资料建成可检索的 SQLite FTS5 索引。"""

from asc_agent.indexing.tokenizer import tokenize, build_match_query

__all__ = ["tokenize", "build_match_query"]
