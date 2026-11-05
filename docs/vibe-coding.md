# Vibe coding 记录：AI 写样板，人写判断

本项目的原则是「用 AI 写样板代码，人专注 system prompt 与编排循环」。这份记录说明
**哪些是 AI 生成的、哪些是人决定的、以及为什么这么分**。

## 1. 分工边界

| 部分 | 谁写 | 理由 |
| --- | --- | --- |
| `indexing/*`（分词、解析、建库、检索打分） | AI 生成 + 人调参 | 样板逻辑清晰，但排序权重要靠实测反复调 |
| `mcp_server/models.py`（Pydantic 契约） | AI 生成 | 纯机械：字段名 + 描述 + 约束 |
| `mcp_server/tools.py`（13 个只读工具） | AI 生成骨架 + 人补业务判断 | 函数体是「查库 → 组装 → 返回」，但冲突判定、prototype 粒度、兜底策略是人定的 |
| `mcp_server/server.py`（MCP 装配） | AI 生成 | `MCPServer` / `@tool` / `@resource` / `@prompt` 的标准用法 |
| `agent/bridge.py`（MCP client） | AI 生成 | `list_tools → OpenAI tools` 的转换是模板工作 |
| `agent/llm.py`（客户端） | AI 生成 | OpenAI 兼容接口调用 |
| **`agent/prompts.py`（系统提示词）** | **人写** | 决定 agent 行为的上限，无法外包 |
| **`workflows.py`（8 个工作流的注册表）** | **人写** | 取证顺序是业务判断，且要同时服务 agent 与 MCP 两层 |
| **`agent/orchestrator.py`（循环语义）** | **人写** | 轮数预算、结果回灌策略、失败收敛，是工程判断 |
| `tests/*` | AI 生成 + 人补断言 | 结构由 AI 铺，具体期望值（该命中哪个文件）必须人看 |
| `docs/*`、`README.md` | AI 生成初稿 + 人改事实 | —— |

一句话概括：**凡是「换个资料库就要重写」的是 AI 写的；凡是「换个资料库照样成立」
的是人写的。**

## 2. 具体的 AI 生成片段

### 2.1 MCP server 装配（`server.py`）

让 AI 按 `mcp` 2.3.0 的签名生成的骨架：

```python
srv = MCPServer(name="asc-doc-agent", instructions=INSTRUCTIONS, version="0.1.0")
srv.add_tool(search_docs, name="search_docs", description="...")

@srv.resource("skill://{name}/{path}", name="skill", mime_type="text/markdown")
def skill_resource(name: str, path: str) -> str: ...

@srv.prompt(name="lookup_api", description="查一个 API 怎么用")
def prompt_lookup_api(api: str, chip: str = "") -> str: ...
```

这段是纯样板，AI 一次生成即用——但**踩坑也在这里**：`mcp` 2.x 把 `FastMCP`
改名成 `MCPServer`，导入路径从 `mcp.server.fastmcp` 变成 `mcp.server.mcpserver`。
报错信息本身指路（"FastMCP was renamed to MCPServer, see the migration guide"），
AI 读报错就能改对。

### 2.2 Pydantic 契约（`models.py`）

```python
class SearchRequest(BaseModel):
    query: str = Field(description="用户的自然语言问题，例如「怎么做 fp32 转 bf16」")
    top_k: int = Field(default=6, ge=1, le=20, description="返回片段数")
    kind: DocKind = Field(default=DocKind.any, description="限定条目类型")
```

AI 生成的字段约束（`ge/le`）后来救了一次：`top_k=999` 在 MCP schema 层就被拦下，
不会跑到 SQL 里。

### 2.3 文档检查器的正则（`lint.py`）

规则文本直接来自 `asc-doc-checker/SKILL.md`，让 AI 把自然语言规范翻译成正则：

```python
SPACE_CJK_EN = re.compile(r"[\u4e00-\u9fff][ \t]+[A-Za-z0-9]|[A-Za-z0-9][ \t]+[\u4e00-\u9fff]")
HALF_PUNCT_IN_CJK = re.compile(r"[\u4e00-\u9fff][,;:!?](?=[\u4e00-\u9fff\s]|$)")
```

第一版 `HALF_PUNCT_IN_CJK` 写的是 `[,.;:!?](?=\s|$)`，导致 `调用,这里` 检不出来
（逗号后面直接接中文）。这是**测试驱动发现**的：写了 `test_lint_detects_english_punct_and_spacing`
才发现漏检，回头改前瞻条件。

## 3. 人做的关键判断（AI 给不出的部分）

### 3.1 提示词只写纪律，不写知识

最初让 AI 起草提示词，它写进来一堆事实：

> 「Ascend 910B 的 `__NPU_ARCH__` 是 2201，支持 fp16/bf16/fp32…」

这是**错的**——资料一更新就腐化，而且模型会拿这段记忆覆盖工具返回的实时事实。
全部删掉，换成六条纪律（先取证、架构号只来自工具、冲突双报、注意 prototype 粒度、
换词最多两轮、区分事实与建议），每条都对应一次真实事故。

### 3.2 工具签名必须是扁平的

AI 最初按「最 Pythonic」的方式写：

```python
def search_docs(args: SearchRequest) -> dict: ...
```

生成的 MCP schema 是 `{"properties": {"args": {"$ref": "#/$defs/SearchRequest"}}}`。
`$ref` 嵌套在 function calling 里容易调错。改成扁平参数后 schema 变干净。
这个判断 AI 做不出来——它不知道下游是 function calling。

### 3.3 冲突判定要逐文档做

AI 写的第一版冲突检测是「聚合比较」：

```python
if chip in csv_yes and all(not x["supported"] for x in by_chip[chip]):
    conflicts.append(...)
```

跑出来 `Exp` 永远报冲突，因为 CSV 在 `ascend910b` 下同时有 yes 和 no 两行——
它们对应**两篇不同文档**（regbase 版不支持、membase 版支持），不是矛盾。
必须改成逐文档比对（CSV 行的 `path` ↔ 同路径文档的标签），并要求 CSV 路径
先归一成仓库全路径（310b 的 CSV 用短路径，910b 用全路径，必须补
`docs/zh/api/SIMD-API/` 前缀才能 JOIN）。

### 3.4 循环的收尾策略

AI 写的第一版是「轮数用完直接返回空字符串」。人改成：追加一条
「预算用完，只根据已有证据作答，开头说明还缺什么」的 user 消息，再调一次
（不带 tools）。理由是**半成品证据比没有答案有用**——用户至少知道查到哪一步。

## 4. 迭代过程（时间线）

| 阶段 | 做了什么 | 产出 |
| --- | --- | --- |
| 1 | 勘察 asc-devkit 结构、读 4 个 SKILL.md、确认 mcp SDK 版本 | 确定「用 `.agent` 技能做事实源」的路线 |
| 2 | 让 AI 生成 tokenizer / markdown_parser / indexer | 建库 3 秒、3676 篇 / 27355 块 |
| 3 | 人调排序（两趟召回 + 名次归一 + 文件名加权） | `怎么做精度转换` 从无关结果修正到 `precision_conversion.md` |
| 4 | 让 AI 生成 13 个只读工具的骨架 | 工具层跑通 |
| 5 | 人补冲突判定、prototype 粒度、兜底策略 | `api_support` 不再误报冲突 |
| 6 | 让 AI 生成 MCP 装配 + client 桥 | 端到端 HTTP 往返跑通 |
| 7 | 人写提示词与编排循环 | 工作流、轮数预算、轨迹 |
| 8 | 让 AI 生成测试，人补期望值 | 81 个测试全绿 |
| 9 | 修线程安全 / 日志噪音 / 连接重置三个工程坑 | CLI 输出干净 |

## 5. 三个「AI 生成物必须人来验」的例子

1. **`--output-dir` 参数**：AI 按名字理解为「输出目录」，照抄进 `cmd` 列表。
   实测生成了一个 4901 字节的**文件**叫 `data/ut_out`——生成器把 `--output-dir`
   当输出路径用了。改用 `--output <file>`。
2. **`--core` 参数**：AI 根据技能文档的 `<芯片> <API类型> <API名称> [aic|aiv]`
   参数顺序，推断生成器有 `--core`。实际没有，核心类型只能通过 `--type aiv|aic`
   表达。跑一次 `--list-supported` 才发现。
3. **uvicorn 日志**：AI 生成的 `_quiet()` 只静音了 `httpx`，但 mcp 2.x 依赖的是
   **`httpx2`**；而且 `uvicorn.Config` 默认会 `dictConfig` 把级别改回 INFO。
   两处都要改才安静。

**共同的教训**：AI 对「第三方工具的接口语义」只能猜，必须跑一次看真实行为。
所以本项目里所有对外部工具（UT 生成器、gitcode、MCP SDK）的调用，都先做一次
最小可运行验证，再写进代码。

## 6. 复现这份工作的方法

```bash
# 1) 让 AI 读技能，而不是读你的脑子
#    把 .agent/skills/*/SKILL.md 和 references/ 直接给它，事实源唯一

# 2) 先验证第三方接口，再让 AI 写调用代码
python .agent/skills/asc-api-ut-gen/scripts/ut_generator_cli.py --list-supported

# 3) 样板代码一次性生成，然后用测试反推期望值
python -m pytest tests/ -q

# 4) 提示词人工写，用 --trace 观察模型实际走了哪条路
python -m asc_agent ask "..." --trace
```
