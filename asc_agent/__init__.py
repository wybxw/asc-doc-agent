"""CANN 算子 / 硬件资料助手。

三个子包：
* `indexing`   —— 把 asc-devkit 的资料解析成 SQLite FTS5 索引（检索层）
* `mcp_server` —— 用 MCP 把检索、支持性、芯片事实、文档规范检查等能力暴露成工具
* `agent`      —— 提示词 + 编排循环，把工具串成常见工作流
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
