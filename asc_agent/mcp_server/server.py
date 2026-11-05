"""MCP server 装配：把工具、资源、提示模板挂到 MCPServer 上。

传输层选择：**streamable-http**（默认 127.0.0.1:8931/mcp）。
不默认用 stdio 的原因写在 README 里：本机沙箱下 asyncio 建管道会被拒
（WinError 5），而纯 HTTP 传输在本地开发和远程复用上都更省事。
依然保留 `--transport stdio` 供标准客户端（Claude Desktop 等）使用。

Vibe coding 说明：这个文件的骨架（MCPServer / @tool / @resource / @prompt
的用法）是让 AI 按 mcp 2.x 的签名直接生成的，人只看「工具描述写得够不够
让模型自己选对工具」。
"""

from __future__ import annotations

import argparse
import sys

from mcp.server.mcpserver import MCPServer

from asc_agent import config
from asc_agent.mcp_server import facts, tools
from asc_agent.mcp_server.docs import read_skill
from asc_agent.mcp_server.tools import (
    api_card,
    api_support,
    check_markdown,
    chip_apis,
    chip_facts,
    fetch_url,
    find_examples,
    find_symbol,
    gen_ut,
    read_doc,
    repo_stats,
    search_docs,
)
from asc_agent.mcp_server.tools_kb import (
    card_check,
    card_delete,
    card_get,
    card_link,
    card_save,
    card_search,
)

INSTRUCTIONS = """\
你是「CANN 算子/硬件资料助手」。你的知识边界以本地 asc-devkit 资料库为准：
**任何关于 AscendC API 用法、芯片架构号、数据精度、文档规范、UT 生成的问题，
都必须先调用工具取证，再作答。**

工作纪律：
1. 先用 search_docs 定位，再用 read_doc / api_card 取证；引用必须带出处路径。
2. 架构号、SocVersion、dtype 位宽一律用 chip_facts 查，不许凭记忆写 2201/3510。
3. 支持性有冲突（文档标签 vs 支持性 CSV）时，两个来源都报出来。
4. 工具返回 found=false 时，换关键词再试一次，仍无结果就说「资料库无记录」，
   并给出下一步建议（用哪个工具、看哪类资料），不要编造。
5. 回答用中文，先给结论再给证据；代码块标注语言。
"""


def build_server() -> MCPServer:
    srv = MCPServer(
        name="asc-doc-agent",
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )

    # ---- 工具 ------------------------------------------------------------
    srv.add_tool(
        search_docs, name="search_docs",
        description="检索 CANN/AscendC 资料库（API 文档/指南/样例/技能），返回带出处的片段。",
    )
    srv.add_tool(
        api_card, name="api_card",
        description="取单个 API 的结构化卡片：函数原型、参数、头文件、芯片支持矩阵。",
    )
    srv.add_tool(
        api_support, name="api_support",
        description="查询某 API 在各芯片上的支持情况（合并文档标签与支持性 CSV）。",
    )
    srv.add_tool(
        chip_apis, name="chip_apis",
        description="反查某芯片支持哪些 API（基于仓库自带支持性 CSV）。",
    )
    srv.add_tool(
        chip_facts, name="chip_facts",
        description="查芯片架构事实（__NPU_ARCH__/SocVersion/实现目录）与数据类型规格。",
    )
    srv.add_tool(
        read_doc, name="read_doc",
        description="按路径读资料原文，可只取指定章节，避免读爆上下文。",
    )
    srv.add_tool(
        read_skill, name="read_skill",
        description="读取 .agent/skills 下的技能说明或参考文档（agent 的行为规范来源）。",
    )
    srv.add_tool(
        fetch_url, name="fetch_url",
        description="抓取在线网页/上游文档并清洗成纯文本。",
    )
    srv.add_tool(
        check_markdown, name="check_markdown",
        description="按 asc-doc-checker 规范检查 Markdown（标点/标题/代码块/芯片标签/章节顺序/术语）。",
    )
    srv.add_tool(
        find_symbol, name="find_symbol",
        description="在 include/impl/examples 源码中定位符号定义（补 Markdown 索引的盲区）。",
    )
    srv.add_tool(
        find_examples, name="find_examples",
        description="按关键字查找可运行的样例工程目录。",
    )
    srv.add_tool(
        gen_ut, name="gen_ut",
        description="调用 asc-api-ut-gen 生成 gtest 测试骨架（只落盘，不编译）。",
    )
    srv.add_tool(
        repo_stats, name="repo_stats",
        description="查看索引规模与可用技能列表。",
    )

    # ---- 知识卡片：个人维护的经验（可写） --------------------------------
    srv.add_tool(
        card_search, name="card_search",
        description="检索个人知识卡片（官方文档里没有的经验、约束、坑、选型结论）。",
    )
    srv.add_tool(
        card_get, name="card_get",
        description="读一张知识卡片全文 + 关系图（出边/入边）。",
    )
    srv.add_tool(
        card_save, name="card_save",
        description="新建或更新知识卡片（写入 knowledge/cards/<id>.md）。更新只改传入字段。",
    )
    srv.add_tool(
        card_link, name="card_link",
        description="建立/删除卡片关系：refines/generalizes/depends_on/supersedes/"
        "contradicts/alternative/related。",
    )
    srv.add_tool(
        card_delete, name="card_delete",
        description="删除知识卡片（需 confirm=true；结论被取代时应标 deprecated 而非删除）。",
    )
    srv.add_tool(
        card_check, name="card_check",
        description="知识库体检：悬空关系、自环、取代未标 deprecated、作用域笔误等。",
    )

    # ---- 资源：把技能文件暴露成可枚举的 URI --------------------------------
    @srv.resource(
        "skill://{name}/{path}",
        name="skill",
        description="读取 asc-devkit/.agent/skills 下的技能文件",
        mime_type="text/markdown",
    )
    def skill_resource(name: str, path: str) -> str:
        data = read_skill(name, path)
        if not data.get("found"):
            return f"未找到技能文件：{name}/{path}"
        return data["body"]

    @srv.resource(
        "chip://facts/{chip}",
        name="chip-facts",
        description="芯片架构事实（来自 asc-npu-arch 技能的 npu-arch-facts.json）",
        mime_type="application/json",
    )
    def chip_resource(chip: str) -> str:
        import json

        return json.dumps(facts.chip_facts(chip), ensure_ascii=False, indent=2)

    # ---- 提示模板：从 asc_agent.workflows 单一事实源注册 ------------------
    # 不在这里手写 prompt 正文——内容全部来自工作流注册表，这样「加一个工作流」
    # 只改一个文件，agent 侧与 MCP 侧不会再漂移。
    from asc_agent.workflows import prompt_specs

    try:
        from mcp.server.mcpserver.prompts import Prompt
        from mcp.server.mcpserver.prompts.base import PromptArgument
    except ImportError:  # pragma: no cover - SDK 路径变化时不致命，只是少 7 个 prompt
        Prompt = None  # type: ignore[assignment]
        print(
            "[asc-doc-agent] 警告：当前 mcp SDK 未提供 Prompt 类，"
            "工作流 prompt 不会注册（工具与资源不受影响）",
            file=sys.stderr,
        )

    if Prompt is not None:
        for spec in prompt_specs():
            srv.add_prompt(
                Prompt(
                    name=spec["name"],
                    title=spec["title"],
                    description=spec["description"],
                    arguments=[
                        PromptArgument(
                            name=a["name"],
                            description=a["description"],
                            required=a["required"],
                        )
                        for a in spec["arguments"]
                    ],
                    fn=spec["render"],
                )
            )

    return srv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="asc-doc-agent MCP server")
    parser.add_argument("--transport", default="streamable-http",
                        choices=["stdio", "sse", "streamable-http"])
    parser.add_argument("--host", default=config.MCP_HOST)
    parser.add_argument("--port", type=int, default=config.MCP_PORT)
    args = parser.parse_args(argv)

    srv = build_server()
    if args.transport == "stdio":
        srv.run("stdio")
    else:
        print(
            f"[asc-doc-agent] MCP {args.transport} → http://{args.host}:{args.port}{config.MCP_PATH}",
            file=sys.stderr,
            flush=True,
        )
        srv.run(args.transport, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
