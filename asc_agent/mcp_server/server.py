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

    # ---- 提示模板：把「常见工作」固化成可直接执行的工作流 ------------------
    @srv.prompt(
        name="lookup_api",
        description="查一个 API 怎么用、支持哪些芯片",
    )
    def prompt_lookup_api(api: str, chip: str = "") -> str:
        return f"""\
请查清 API `{api}`{f' 在 `{chip}` 上的情况' if chip else ''}，按下面顺序取证：

1. `api_card(name="{api}")` —— 拿原型、参数表、头文件、支持矩阵。
2. 若卡片缺失（found=false），`search_docs(query="{api} 函数原型")` 再补。
3. {f'`api_support(api="{api}", chip="{chip}")` 核对支持性。' if chip else f'`api_support(api="{api}")` 列出各芯片支持情况。'}
4. 需要确认头文件时 `find_symbol(name="{api}", scope="include")`。

输出：一句话结论 → 函数原型（cpp 代码块）→ 参数说明表 → 支持芯片 →
头文件路径 → 出处（每段都标 path）。
"""

    @srv.prompt(
        name="chip_capability",
        description="某芯片支持什么：架构号、API 范围、数据类型",
    )
    def prompt_chip_capability(chip: str) -> str:
        return f"""\
请汇总芯片 `{chip}` 的能力画像：

1. `chip_facts(chip="{chip}")` —— 架构号、SocVersion、实现目录。
2. `chip_apis(chip="{chip}", only_supported=true)` —— 支持性 CSV 覆盖范围内的 API 清单。
3. 涉及低精度时 `chip_facts(chip="{chip}", dtype="fp8_e4m3fn_t")` 核对。
4. 结论里明确区分「CSV 有记录」与「只有文档标签」两类证据。

输出：架构速查表 → 支持能力概览 → 资料缺口（哪些结论没有 CSV 支撑）。
"""

    @srv.prompt(
        name="write_api_doc",
        description="为新 API 写一份符合规范的文档并自检",
    )
    def prompt_write_api_doc(api: str) -> str:
        return f"""\
为 API `{api}` 写一份符合 asc-doc-checker 规范的文档：

1. `read_skill(skill="asc-doc-checker")` 读规范；需要细节再读
   `read_skill(skill="asc-doc-checker", file="references/writing_specs.md")`。
2. `api_card(name="{api}")` 取真实原型与参数（不要编造签名）。
3. 按固定章节顺序写：产品支持情况 → 功能说明 → 函数原型 → 参数说明 →
   数据类型（可选）→ 返回值说明 → 约束说明 → 调用示例。
4. 芯片标签用 `<!-- npu="950" id1 -->` 形式，组内顺序 950→A3→910b→910→310p→310b→x90→9030。
5. 写完把文件落到 docs/ 下，再 `check_markdown(path=...)` 自检并把问题逐条修掉。
"""

    @srv.prompt(
        name="gen_ut_for_api",
        description="为 API 生成 UT 骨架",
    )
    def prompt_gen_ut_for_api(api: str, chip: str = "ascend910b1", api_type: str = "membase") -> str:
        return f"""\
为 API `{api}` 生成 UT（芯片 `{chip}`，类型 `{api_type}`）：

1. `chip_facts(chip="{chip}")` 确认芯片调用名合法。
2. `gen_ut(api="{api}", api_type="{api_type}", chip="{chip}")`。
3. 若返回 ok=false，把生成器的报错**原样**转述（它说明该 API 没有可泛化的签名），
   然后 `find_symbol(name="{api}", scope="impl")` 与 `find_examples(keyword="{api.lower()}")`
   给出「先读哪个 impl、抄哪个样例」的具体建议。
4. ok=true 时说明产出路径、行数，并提示需要人工补齐的 tiling/数据初始化部分。
"""

    @srv.prompt(
        name="review_markdown",
        description="检查一篇文档是否符合规范",
    )
    def prompt_review_markdown(path: str) -> str:
        return f"""\
检查文档 `{path}` 是否符合 asc-doc-checker 规范：

1. `check_markdown(path="{path}")` 跑机器可判定的 14 条规则。
2. 对机器判不了的部分（表述是否准确、图是否符合意图），`read_skill` 读规范后人工判。
3. 输出：结论（通过/不通过）→ 按规则分组的问题清单（带行号与原文片段）→ 逐条修改建议。
4. 不要只说「有空格问题」，要给出「第 N 行 `xxx` 应改为 `yyy`」。
"""

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
