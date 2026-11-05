"""MCP 桥：把 MCP server 的工具清单翻译成 LLM 的 function tools，并转发调用。

职责边界很清楚：
* 上游 → 取 `list_tools()` 的 name/description/input_schema，原样塞进
  `{"type":"function","function":{...}}`（MCP 的 input_schema 就是 JSON Schema，
  和 OpenAI function calling 的 parameters 同构，几乎零转换）。
* 下游 → `call_tool(name, arguments)`，把 MCP 返回的 content 拼成文本给模型。

传输用 streamable-http。本地默认由 `LocalServer` 在后台线程起一个 uvicorn，
也可以 `--server-url` 连到已经在跑的实例（多个 agent 共用一份索引）。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from typing import Any

import httpx

from asc_agent import config


def _silence_connection_reset(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """过滤 Windows 上收尾阶段的 ConnectionResetError 噪音。"""
    exc = context.get("exception")
    if isinstance(
        exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
    ):
        return
    loop.default_exception_handler(context)


def pick_port(preferred: int) -> int:
    """端口被占时顺延，避免演示时因为残留进程起不来。"""
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"{preferred}..{preferred + 19} 全部被占用")


def server_url(host: str, port: int) -> str:
    return f"http://{host}:{port}{config.MCP_PATH}"


class LocalServer:
    """在本进程的后台线程里跑 MCP server（uvicorn）。

    选择线程而不是子进程：沙箱里子进程的 stdio 管道会被拒（WinError 5），
    线程 + 纯 HTTP 传输完全绕开这个问题，且启动/退出都不留孤儿进程。
    """

    def __init__(self, host: str = config.MCP_HOST, port: int | None = None):
        self.host = host
        self.port = port or pick_port(config.MCP_PORT)
        self._thread: threading.Thread | None = None
        self._server: Any = None

    @property
    def url(self) -> str:
        return server_url(self.host, self.port)

    def start(self, timeout: float = 20.0) -> "LocalServer":
        import uvicorn

        from asc_agent.mcp_server.server import build_server

        app = build_server().streamable_http_app(streamable_http_path=config.MCP_PATH)
        cfg = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            # 不接管 logging：uvicorn 默认会 dictConfig 重配日志级别，
            # 把调用方设好的 WARNING 覆盖回 INFO，导致每次 HTTP 往返都刷屏。
            log_config=None,
        )
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(
            target=self._server.run, name="mcp-server", daemon=True
        )
        self._thread.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((self.host, self.port), timeout=0.4):
                    return self
            except OSError:
                time.sleep(0.15)
        raise RuntimeError(f"MCP server 未在 {timeout}s 内就绪：{self.url}")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


class McpBridge:
    """一个 MCP 会话的同步门面（编排循环是同步的，内部用独立事件循环跑异步）。"""

    def __init__(self, url: str):
        self.url = url
        self._tools: list[dict[str, Any]] = []

    # ---- 生命周期 --------------------------------------------------------
    def __enter__(self) -> "McpBridge":
        self._loop = asyncio.new_event_loop()
        # Windows 的 proactor 循环在连接被对端关掉时会往 stderr 打
        # ConnectionResetError（连接被强制关闭）。这属于正常收尾噪音，不是
        # 错误，装个异常处理器吞掉，避免污染 CLI 输出。
        self._loop.set_exception_handler(_silence_connection_reset)
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._stop_evt = asyncio.Event()
        asyncio.run_coroutine_threadsafe(self._main(), self._loop)
        if not self._ready.wait(timeout=30):
            raise RuntimeError("连接 MCP server 超时")
        if self._error:
            raise self._error
        return self

    def __exit__(self, *exc_info) -> None:
        self._loop.call_soon_threadsafe(self._stop_evt.set)
        self._thread.join(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _main(self) -> None:
        from mcp.client.streamable_http import streamable_http_client
        from mcp import ClientSession

        try:
            async with streamable_http_client(self.url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    listed = await session.list_tools()
                    self._tools = [
                        {
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "description": (t.description or "").strip()[:1024],
                                "parameters": t.input_schema or {"type": "object", "properties": {}},
                            },
                        }
                        for t in listed.tools
                    ]
                    self._ready.set()
                    await self._stop_evt.wait()
        except BaseException as exc:  # noqa: BLE001 - 传给主线程再抛
            self._error = exc
            self._ready.set()

    # ---- 对编排循环的接口 ------------------------------------------------
    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._tools

    def names(self) -> list[str]:
        return [t["function"]["name"] for t in self._tools]

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        """调用工具，返回给模型的文本（JSON 字符串）。"""
        fut = asyncio.run_coroutine_threadsafe(self._call(name, arguments), self._loop)
        return fut.result(timeout=180)

    async def _call(self, name: str, arguments: dict[str, Any]) -> str:
        result = await self._session.call_tool(name, arguments)
        chunks: list[str] = []
        for item in result.content:
            text = getattr(item, "text", None)
            if text is not None:
                chunks.append(text)
            else:
                chunks.append(json.dumps({"type": getattr(item, "type", "?")}, ensure_ascii=False))
        body = "\n".join(chunks) if chunks else "{}"
        if result.is_error:
            return json.dumps({"tool_error": True, "detail": body}, ensure_ascii=False)
        return body

    def read_resource(self, uri: str) -> str:
        try:
            fut = asyncio.run_coroutine_threadsafe(self._read(uri), self._loop)
            return fut.result(timeout=60)
        except Exception as exc:  # noqa: BLE001
            return f"读取资源失败（{uri}）：{type(exc).__name__}: {exc}"

    async def _read(self, uri: str) -> str:
        res = await self._session.read_resource(uri)
        return "\n".join(getattr(c, "text", "") or "" for c in res.contents)


def wait_http_ready(url: str, timeout: float = 20.0) -> bool:
    """轮询一个 HTTP 端点，确认外部 server 已经起来。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url.replace("/mcp", "/"), timeout=1.0)
            if r.status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    return False
