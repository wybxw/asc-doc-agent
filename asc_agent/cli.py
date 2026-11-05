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

# 多轮对话保留的历史轮数（1 轮 = user + assistant 两条消息）
MAX_HISTORY_TURNS = 6


def _banner(msg: str) -> None:
    print(f"\033[36m{msg}\033[0m", file=sys.stderr)


def _check_workflow(name: str) -> str:
    """校验 --workflow，改写成规范 key。

    以前无法识别的工作流会被静默降级成「自动路由」，用户以为指定了其实没有。
    显式指定的东西不该被悄悄忽略，所以这里直接报错并列出可选值。
    """
    from asc_agent.workflows import WORKFLOWS, WORKFLOW_ALIASES, resolve_workflow

    if not name:
        return ""
    key = resolve_workflow(name)
    if key:
        if key != name:
            _banner(f"工作流 {name!r} → {key}")
        return key
    valid = ", ".join(WORKFLOWS)
    aliases = ", ".join(sorted(WORKFLOW_ALIASES))
    print(
        f"未知工作流：{name!r}\n可选：{valid}\n别名：{aliases}",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _make_agent(args):
    from asc_agent.agent.bridge import LocalServer, McpBridge
    from asc_agent.agent.llm import make_client
    from asc_agent.agent.orchestrator import Agent

    server = None
    url = args.server_url
    if not url:
        try:
            server = LocalServer(port=args.port).start()
        except Exception as exc:  # noqa: BLE001
            print(f"启动本地 MCP server 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            print("可用 --port 换个端口，或先 `serve` 起来再用 --server-url 指过去。", file=sys.stderr)
            raise SystemExit(1)
        url = server.url
        _banner(f"MCP server: {url}")
    try:
        bridge = McpBridge(url).__enter__()
    except Exception as exc:  # noqa: BLE001
        if server:
            server.stop()
        print(
            f"连接 MCP server 失败（{url}）：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    client = make_client(offline=args.offline)
    _banner(f"LLM: {getattr(client, 'name', type(client).__name__)}  工具: {len(bridge.tools)} 个")
    return Agent(bridge, client), bridge, server


def cmd_ask(args) -> int:
    workflow = _check_workflow(args.workflow)
    agent, bridge, server = _make_agent(args)
    try:
        answer, trace = agent.run(args.question, workflow=workflow)
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
    workflow = _check_workflow(args.workflow)
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
            answer, trace = agent.run(question, workflow=workflow, history=history)
            print(answer)
            if args.trace:
                print(json.dumps(trace.summary(), ensure_ascii=False), file=sys.stderr)
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": answer})
            # 只保留最近 6 轮：多轮对话不裁剪的话，上下文会无限增长，
            # 既烧 token 又容易把早期无关内容带进当前问题。
            if len(history) > MAX_HISTORY_TURNS * 2:
                history[:] = history[-MAX_HISTORY_TURNS * 2:]
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


def cmd_cards(args) -> int:
    """知识卡片运维：列表 / 体检。写卡片请用 agent（--workflow knowledge_card）。"""
    from asc_agent.knowledge import store

    if args.check:
        result = store.check()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    cards = store.list_cards()
    if not cards:
        print(f"知识库为空：{store.display_path(config.cards_root())}")
        return 0
    for c in cards:
        scope = " ".join(
            f"{k}={','.join(v)}" for k, v in c.scope.model_dump().items() if v
        ) or "通用"
        # 入边是推导出来的，计数要算上，否则看起来像「没人引用」
        graph = store.graph(c)
        print(f"- \033[1m{c.id}\033[0m  [{c.type.value}/{c.status.value}]  {c.title}")
        print(f"    {c.summary or '(无 summary)'}")
        print(
            f"    作用域：{scope}  证据：{len(c.evidence)} 条  "
            f"关系：{len(graph['outgoing'])} 出 / {len(graph['incoming'])} 入"
        )
    print(f"\n共 {len(cards)} 张，目录 {store.display_path(config.cards_root())}")
    print("体检：python -m asc_agent cards --check")
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

    sp = sub.add_parser("cards", help="列出知识卡片 / 体检")
    sp.add_argument("--check", action="store_true", help="跑一致性体检（悬空关系、取代未标 deprecated 等）")
    sp.set_defaults(func=cmd_cards)

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
