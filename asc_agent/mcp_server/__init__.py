"""MCP server 包：工具契约（models）、事实层（facts）、工具实现（tools）。

这里刻意**不** re-export `build_server`：`server.py` 需要导入本包的子模块，
在 `__init__` 里反向导入会形成循环。需要装配服务请直接
`from asc_agent.mcp_server.server import build_server`。
"""

__all__: list[str] = []
