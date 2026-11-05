# asc-doc-agent

**CANN 算子 / 硬件资料助手**——一个只干一件事的 agent：把 `asc-devkit` 里散落在
3676 篇 Markdown、2889 个 API 文档、2468 行支持性 CSV 和 4 个 `.agent` 技能中的
事实，用自然语言准确、可追溯地取出来。

它不靠模型记忆回答问题。任何涉及 API 原型、参数、头文件路径、芯片支持性、
架构号、dtype 位宽的话，都必须来自一次真实的工具调用，回答里要带出处。

```text
$ python -m asc_agent ask "Exp 在 910b 上支持吗" --offline --trace

**Exp**：文档标签支持：... ；CSV 支持：ascend910b；CSV 不支持：ascend910b
- docs/zh/api/SIMD-API/basic_api/memory_vector_compute/basic_arithmetic/Exp.md：
  ascend910b 1/2（支持数/原型数）
- docs/zh/api/SIMD-API/basic_api/reg_vector_compute/basic_arithmetic/Exp.md：
  ascend910b 0/1（支持数/原型数）
```

---

## 1. 设计目标与实现对照

| 设计目标 | 本项目的实现 |
| --- | --- |
| **工具面：多个不同功能的 skill / 工具** | MCP 暴露 **19 个**工具：13 个只读资料工具，覆盖 5 类能力（①资料检索：中文 bigram 全文索引 + 意图重排 ②结构化事实：API 卡片 / 芯片架构 / dtype 规格 ③双源支持性比对：文档 `npu` 标签 × 仓库 CSV ④文档规范仪器化检查：asc-doc-checker 的规则变成 10 组可执行断言 ⑤源码定位与工具链调用：头文件符号、样例工程、UT 生成器）；另有 6 个可写的个人知识卡片工具 |
| **上下文集成：用标准协议打通模型与本地环境** | 标准 **MCP**（Model Context Protocol，`mcp` SDK 2.3.0）：`asc_agent/mcp_server` 是 server（19 tools + 2 resource templates + 8 prompts，streamable-http 传输），`asc_agent/agent/bridge.py` 是 client。工具清单经 MCP 握手自动转成 LLM 的 function schema，**没有手写第二份工具定义** |
| **开发方式：AI 写样板，人专注 prompt 与编排** | 索引器、Pydantic 契约、MCP 装配、规则正则、CLI、测试全部由 AI 生成；人工只定 `asc_agent/agent/prompts.py`（纪律）、`asc_agent/workflows.py`（工作流）和 `asc_agent/agent/orchestrator.py`（循环语义）。过程记录见 [`docs/vibe-coding.md`](docs/vibe-coding.md) |
| **复用既有 `.agent` 技能而非重写** | 4 个技能被**原样复用**：`asc-npu-arch` 的 `npu-arch-facts.json` 是唯一芯片事实源（`facts.py` 只读、零复制）；`asc-doc-checker` 的 308 行规范变成 `lint.py`；`asc-api-ut-gen` 的生成器被 `gen_ut` 直接调用；`asc-ar-dev` 的输出契约写进系统提示词 |

## 2. 快速开始

```bash
cd /path/to/asc-doc-agent        # 仓库根，asc-devkit/ 放在同级
pip install -r requirements.txt

# 1) 建索引（约 3 秒）
python -m asc_agent index

# 2) 离线跑通整条链路（不需要任何 API key）
python -m asc_agent ask "Cast 怎么用" --offline --trace

# 3) 接真实模型（任意 OpenAI 兼容端点）
export ASC_AGENT_API_KEY=sk-xxx
export ASC_AGENT_BASE_URL=https://api.deepseek.com/v1
export ASC_AGENT_MODEL=deepseek-flash     # DeepSeek 官方：deepseek-flash / deepseek-v4-pro
python -m asc_agent ask "Matmul 的 tiling 怎么配" --trace
python -m asc_agent chat                  # 多轮（自动保留最近 6 轮上下文）
```

> 模型名以服务端 `/v1/models` 为准，写错会直接报 model not found。
> 也可换通义 / vLLM / Ollama 等任何 OpenAI 兼容端点，只改 `ASC_AGENT_BASE_URL`。
> 不加 `--offline` 且缺 key 时会退回离线模式并提示。

常用子命令：`ask`（单问）、`chat`（多轮）、`tools`（列工具）、`workflows`（列工作流）、
`serve`（只起 MCP server，供 Claude Desktop / Cursor 等客户端连）、`index`（重建索引）。

## 3. 架构

```
                ┌──────────────────────────────────────────────┐
   用户问题 ──▶ │ asc_agent/agent/orchestrator.py              │
                │   system prompt（纪律）+ 工作流步骤 + 工具循环 │
                └───────────────┬──────────────────────────────┘
                                │ OpenAI function calling
                                ▼
                ┌──────────────────────────────────────────────┐
                │ asc_agent/agent/bridge.py（MCP client）       │
                │   list_tools() → LLM tools                   │
                │   call_tool()  → 结果回灌                     │
                └───────────────┬──────────────────────────────┘
                                │ MCP over streamable-http
                                ▼
   ┌────────────────────────────────────────────────────────────┐
   │ asc_agent/mcp_server/server.py（MCPServer）                 │
   │  19 tools · skill://{name}/{path} · chip://facts/{chip}     │
   │  5 prompts（lookup_api / chip_capability / write_api_doc …）│
   └───────┬──────────────────────┬─────────────────────────────┘
           │                      │
           ▼                      ▼
  ┌─────────────────┐   ┌──────────────────────────────────────┐
  │ indexing/        │   │ 直接读 asc-devkit                    │
  │ SQLite FTS5      │   │  .agent/skills（技能原文与事实 JSON） │
  │ 3676 篇 / 27355 块│   │  include/ impl/ examples（源码定位）  │
  │ + 支持性 CSV      │   │  api_*_support.csv（支持矩阵）        │
  └─────────────────┘   └──────────────────────────────────────┘
```

关键取舍：

- **为什么是 streamable-http 而不是 stdio**：stdio 需要父进程创建管道，本机沙箱里
  `_winapi.CreateFile` 会被拒（`WinError 5`）。HTTP 传输没有这个问题，而且
  `serve` 子命令能让多个客户端共用一份常驻索引。`--transport stdio` 仍保留。
- **为什么自己写索引而不上向量库**：资料是强术语的 API 文档，中文 bigram + FTS5
  的召回精度足够，且构建只要 3 秒、零外部依赖、结果可解释（能定位到行号）。
  语义模糊的查询由同义词扩展和意图软路由兜住。
- **为什么工具签名用扁平参数**：Pydantic 模型作为唯一参数会让 MCP 生成
  `{"properties": {"args": {"$ref": ...}}}` 这种嵌套 schema，function calling 容易调错。
  实测扁平 `Annotated[str, Field(...)]` 的 schema 是干净的
  `{"properties": {"query": {...}}}`。

## 4. 内置工作流

`python -m asc_agent workflows` 可列。工作流不是 if/else 分支，而是**注入提示词的
取证步骤**，全部定义在 **`asc_agent/workflows.py`** 这一处，同时驱动 agent 与
MCP 两层：agent 侧注入任务提示词，MCP 侧注册成标准 prompt（任何 MCP 客户端可调）。
新增工作流只改这一个文件。

| 工作流 | 触发场景 | 取证顺序 |
| --- | --- | --- |
| `lookup_api` | 某 API 怎么用、参数、头文件 | `api_card` → `search_docs(example)` → `api_support` → `find_symbol(include)` |
| `chip_support` | 某 API/特性在哪个芯片支持 | `api_support` → `chip_apis` → `chip_facts` |
| `write_api_doc` | 新写/补写 API 文档 | `read_skill(doc-checker)` → `search_docs` 参照 → `api_card` → `check_markdown` |
| `gen_ut` | 要 UT 骨架 | `read_skill(ut-gen)` → `chip_facts` → `gen_ut` → `read_doc` 复核 |
| `locate_code` | 找实现/找样例 | `find_symbol` → `find_examples` → `search_docs(guide)` |
| `learn_concept` | 概念/入门答疑 | `search_docs(guide)` → `search_docs(api_doc)` → `read_doc` |
| `review_markdown` | 文档规范检查 | `check_markdown` → `read_skill(doc-checker)` → 逐条建议 |
| `knowledge_card` | 个人知识卡片增删改查与解释 | `card_search` 查重 → `card_save` → `card_link` 建关系 → `card_check` |

别名（`api`/`chip`/`ut`/`doc`/`code`/`concept`/`review`/`kb` 等）与统一前的旧 MCP 名
（`chip_capability`、`gen_ut_for_api`）都能解析到上表。指定了无法识别的工作流会
直接报错并列出可选值，不再静默降级成自动路由。

> `knowledge_card` 是唯一会写盘的工作流：它写 `knowledge/cards/*.md`（**个人知识**），
> 不碰 `asc-devkit/`（只读上游）。

## 5. 工具一览

`python -m asc_agent tools` 可列（描述来自 MCP 交给模型的同一份 schema）。

**官方资料（只读）**

| 工具 | 作用 |
| --- | --- |
| `search_docs` | 中文全文检索，支持 kind（api_doc/guide/example/skill）与 chip 过滤 |
| `api_card` | 单 API 卡片：原型、参数、头文件、支持矩阵；同名文档给 `alternatives` |
| `api_support` | 双源支持性比对：文档 `npu` 标签 × CSV，**逐文档**判冲突 |
| `chip_apis` | 反查某芯片支持哪些 API |
| `chip_facts` | `__NPU_ARCH__` / SocVersion / 实现目录 / dtype 位宽与对齐 |
| `read_doc` | 按路径读原文，可只取一章 |
| `read_skill` | 读 `.agent/skills` 的技能与参考文档 |
| `fetch_url` | 抓在线页面并清 HTML |
| `check_markdown` | asc-doc-checker 规范的机器可判定部分 |
| `find_symbol` | 在 include/impl/examples 定位符号定义 |
| `find_examples` | 按关键字找样例工程 |
| `gen_ut` | 调 asc-api-ut-gen 生成 gtest 骨架 |
| `repo_stats` | 索引规模与可用技能 |

**个人知识卡片（可读写）**

| 工具 | 作用 |
| --- | --- |
| `card_search` | 检索个人卡片，可按架构/型号/编程模型/API/类型/状态过滤 |
| `card_get` | 读卡片全文 + 关系图（出边/入边） |
| `card_save` | 新建/更新卡片；**只改传入字段**，省略即保留 |
| `card_link` | 建立/删除关系（七种，见 [`knowledge/README.md`](knowledge/README.md)） |
| `card_delete` | 删卡（需 `confirm=true`；被取代时应标 deprecated 而非删除） |
| `card_check` | 体检：悬空关系、自环、取代未标 deprecated、未知编程模型、缺验证记录 |

## 6. 测试

```bash
python -m pytest tests/ -q          # 150 passed
```

- `tests/test_retriever.py`（24）— 分词、排序回归、卡片兜底、支持矩阵粒度、芯片过滤
- `tests/test_tools.py`（48）— 参数校验、错误兜底、lint 各条规则、并发线程安全、UT 生成
- `tests/test_knowledge.py`（34）— 卡片存储、作用域过滤、关系推导、PATCH 语义、体检
- `tests/test_agent_e2e.py`（32）— **真起 MCP server、真走 HTTP**、编排循环控制流、两层工作流一致性

## 7. 目录

```
asc_agent/
  config.py              路径与 LLM 配置（全部可用环境变量覆盖）
  indexing/
    tokenizer.py         中文 bigram 分词 + FTS5 查询构造
    markdown_parser.py   标题/章节/npu 标签/头文件/支持矩阵解析
    indexer.py           建索引（3 秒 / 3676 篇 / 27355 块）
    retriever.py         两趟召回 + 意图重排 + API 卡片 + 支持性聚合
  mcp_server/
    models.py            Pydantic 工具契约（参数 + 结果）
    facts.py             芯片/dtype 事实（只读技能 JSON）
    docs.py              原文直读、技能读取、路径防护
    web.py               网页抓取 + HTML 清洗
    code.py              符号定位、样例工程检索
    lint.py              asc-doc-checker 规范的执行版
    tools.py             13 个官方资料工具（只读）
    tools_kb.py          6 个知识卡片工具（可写）
    server.py            MCPServer 装配：tools / resources / prompts
  knowledge/
    models.py            ★ 卡片模型 + 六种关系定义（INVERSE 反向推导）
    store.py             文件读写、作用域匹配、检索、体检
  agent/
    prompts.py           ★ 系统提示词（人工核心资产）
    orchestrator.py      ★ 编排循环：轮数预算、轨迹、异常兜底
    llm.py               OpenAI 兼容客户端 + 离线 MockClient
    bridge.py            MCP client 桥 + 本地 server 托管
  workflows.py           ★ 8 个工作流的唯一注册表（agent 与 MCP 共用）
  cli.py                 命令行入口（ask/chat/tools/workflows/cards/serve/index）
knowledge/
  README.md              ★ 卡片字段、作用域、关系设计（怎么维护）
  cards/*.md             卡片文件（人手工维护，进 git）
docs/                    架构、工作流、vibe coding 记录、演示输出
scripts/run_demo.py      一键生成离线演示
tests/                   pytest
```

## 8. 知识卡片

官方文档是**外部事实**，个人卡片是**自己的判断**——两者分开存放、分开标注：

- 存储：`knowledge/cards/<id>.md`（YAML frontmatter + Markdown），**文件即真相**，
  可以直接用编辑器改、能 diff、能进 git；
- 作用域：`architecture` / `soc` / `chip` / `programming_model` / `api` / `dtype`
  六个正交维度，留空即「不限」。这样「通用 / 架构特有 / 型号特有 / 编程模型特有」
  可以自由叠加，而不是一个单值枚举；
- 关系：`refines` / `generalizes` / `depends_on` / `supersedes` / `contradicts` /
  `alternative` / `related`，**只写在声明方一次**，反向读取时推导；
- `status`（draft/verified/deprecated）+ `confidence` + `verified_on`（芯片/CANN/日期）
  用来判断卡片是否过期。

维护方式二选一：直接改文件，或让 agent 改（`--workflow knowledge_card`）。
改完跑体检：

```bash
python -m asc_agent cards            # 列出全部卡片
python -m asc_agent cards --check    # 悬空关系 / 取代未标 deprecated / 缺验证记录
```

细节见 [`knowledge/README.md`](knowledge/README.md)。

## 9. 已知边界

- **支持性 CSV 只覆盖 `ascend910b` / `ascend310b` 两代**。问其它芯片时 `chip_apis`
  会明说覆盖范围，让用户改用 `api_support` 看文档标签——不假装有数据。
- **UT 生成器只对已验证签名的 API 泛型生成**。`Exp` 这类会被拒绝，此时工具把
  生成器原文报错转述给用户并给出替代路径，而不是返回一个假骨架。
- **索引只覆盖中文文档**（`docs/zh` + `examples` + `.agent/skills`）。英文文档
  与中文高度重合，需要时改 `config.DOCS_SUBDIR` 重建即可。
- **`fetch_url` 走公网**，在内网环境会失败；返回结构化错误，不影响本地检索。
