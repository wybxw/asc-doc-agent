"""命令行入口：`python -m asc_agent <子命令>`。

子命令：
  ask        单问单答（可指定工作流）
  chat       交互式多轮
  tools      列出 MCP 工具
  workflows  列出内置工作流
  serve      只起 MCP server（给别家客户端连）
  index      重建索引
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from asc_agent import config
from asc_agent.logging_setup import quiet_logs as _quiet


def _banner(msg: str) -> None:
    print(f"\033[36m{msg}\033[0m", file=sys.stderr)


def _make_agent(args):
    from asc_agent.agent.bridge import LocalServer, McpBridge
    from asc_agent.agent.llm import make_client
    from asc_agent.agent.orchestrator import Agent

    server = None
    url = args.server_url
    if not url:
        server = LocalServer(port=args.port).start()
        url = server.url
        _banner(f"MCP server: {url}")
    bridge = McpBridge(url).__enter__()
    client = make_client(offline=args.offline)
    _banner(f"LLM: {getattr(client, 'name', type(client).__name__)}  工具: {len(bridge.tools)} 个")
    return Agent(bridge, client), bridge, server


def cmd_ask(args) -> int:
    agent, bridge, server = _make_agent(args)
    try:
        answer, trace = agent.run(args.question, workflow=args.workflow)
        print(answer)
        if args.trace:
            print("\n--- trace ---", file=sys.stderr)
            print(json.dumps(trace.summary(), ensure_ascii=False, indent=2), file=sys.stderr)
            for s in trace.steps:
                print(
                    f"  [{s.round}] {s.name}({json.dumps(s.arguments, ensure_ascii=False)})"
                    f" ok={s.ok} {s.elapsed_ms}ms {s.chars}B",
                    file=sys.stderr,
                )
    finally:
        bridge.__exit__(None, None, None)
        if server:
            server.stop()
    return 0


def cmd_chat(args) -> int:
    agent, bridge, server = _make_agent(args)
    history: list[dict] = []
    print("输入问题，空行或 Ctrl-C 退出。", file=sys.stderr)
    try:
        while True:
            try:
                question = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not question:
                break
            answer, trace = agent.run(question, workflow=args.workflow, history=history)
            print(answer)
            if args.trace:
                print(json.dumps(trace.summary(), ensure_ascii=False), file=sys.stderr)
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": answer})
    finally:
        bridge.__exit__(None, None, None)
        if server:
            server.stop()
    return 0


def cmd_tools(args) -> int:
    from asc_agent.agent.bridge import LocalServer, McpBridge

    server = None
    url = args.server_url
    if not url:
        server = LocalServer(port=args.port).start()
        url = server.url
    bridge = McpBridge(url).__enter__()
    try:
        for t in bridge.tools:
            fn = t["function"]
            params = ", ".join(fn["parameters"].get("properties", {}))
            print(f"- \033[1m{fn['name']}\033[0m({params})")
            print(f"    {fn['description'].splitlines()[0] if fn['description'] else ''}")
    finally:
        bridge.__exit__(None, None, None)
        if server:
            server.stop()
    return 0


def cmd_workflows(args) -> int:
    from asc_agent.agent.prompts import list_workflows

    for wf in list_workflows():
        print(f"- \033[1m{wf['key']}\033[0m  {wf['title']}")
        print(f"    适用：{wf['when']}")
        print(f"    交付：{wf['deliver']}")
    return 0


def cmd_serve(args) -> int:
    from asc_agent.mcp_server.server import build_server

    build_server().run("streamable-http", host=args.host, port=args.port)
    return 0


def cmd_index(args) -> int:
    from asc_agent.indexing.indexer import build_index

    stats = build_index(verbose=True)
    print(json.dumps(stats.__dict__, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="asc_agent", description="CANN 算子 / 硬件资料助手")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--offline", action="store_true", help="离线演示：不调真模型，用脚本化客户端")
        sp.add_argument("--server-url", default="", help="连已有的 MCP server；默认本进程自起")
        sp.add_argument("--port", type=int, default=config.MCP_PORT, help="本地 MCP 端口")
        sp.add_argument("--trace", action="store_true", help="打印工具调用轨迹")

    sp = sub.add_parser("ask", help="单问单答")
    sp.add_argument("question", help="问题")
    sp.add_argument("--workflow", default="", help="指定工作流，见 `workflows` 子命令")
    common(sp)
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("chat", help="交互式多轮")
    sp.add_argument("--workflow", default="", help="指定工作流")
    common(sp)
    sp.set_defaults(func=cmd_chat)

    sp = sub.add_parser("tools", help="列出 MCP 工具")
    common(sp)
    sp.set_defaults(func=cmd_tools)

    sp = sub.add_parser("workflows", help="列出内置工作流")
    sp.set_defaults(func=cmd_workflows)

    sp = sub.add_parser("serve", help="只起 MCP server")
    sp.add_argument("--host", default=config.MCP_HOST)
    sp.add_argument("--port", type=int, default=config.MCP_PORT)
    sp.set_defaults(func=cmd_serve)

    sub.add_parser("index", help="重建索引").set_defaults(func=cmd_index)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _quiet()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
