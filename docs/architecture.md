# 架构与关键决策

## 1. 三层切分

```
索引层（indexing）  ——「资料在哪、说了什么」
MCP 层（mcp_server）——「把能力做成工具」
Agent 层（agent）   ——「按什么顺序用、怎么说话」
```

分层的判据是**变更理由**，不是代码行数：

- 资料变多/格式变 → 只动 `indexing`；
- 新增一种能力（比如「查编译日志」）→ 只加一个 `tools.py` 函数 + `server.py` 一行注册；
- 回答风格变 → 只改 `prompts.py`；取证顺序变 → 只改 `workflows.py`。

实际开发中这三层的改动次数是 6 : 1 : 1，说明切分是准的。

## 2. 索引层

### 2.1 为什么不用向量库

试过三种方案后选定的：

| 方案 | 问题 |
| --- | --- |
| 纯 `LIKE` 扫描 | 3676 篇 × 平均 5KB，单次查询 2s+ |
| 向量检索（bge + FAISS） | 需要额外 2GB 模型；API 文档全是术语，语义近邻反而不如字面匹配准；且无法解释「为什么召回」 |
| **FTS5 + 中文 bigram** | 建库 3s、查询 10ms、结果能定位到行、零外部依赖 |

关键实现：`unicode61` 分词器会把整段中文当一个 token，所以**入库前**先用
`tokenize()` 把中文切成二元组（`精度转换` → `精度 度转 转换`），**查询前**用同一
规则切并把多字词包成 FTS5 phrase（`"精度 度转 转换"`）。phrase 保证「相邻才算命中」，
避免「精度」匹配到「精密度」之外的东西。

### 2.2 排序：两趟召回 + 名次归一

早期版本直接用 `-bm25()` 当主分，结果被 bm25 的极端负值（-42 量级）压过所有
启发式，`怎么做精度转换` 排到了无关的 `asc_unpack.md`。现在：

```
final = 10/(1+rank)            # 第一趟 FTS 名次，归一化到 0..10
      + coverage * 6.0          # 去虚词后关键词的覆盖度
      + api_name 前缀命中 3.5
      + 文件名 == API 名 9.0    # 「Matmul 支持哪些芯片」靠这个才不被通用章节盖过
      + 章节先验 / 意图软路由 / 负向信号
```

第二趟用同义词扩展只补召回、不加分，所以「怎么/如何」这类虚词不会污染排序。

### 2.3 支持矩阵必须保留 prototype 粒度

`Exp.md` 里有「不传入 config 的原型」和「传入 config 的原型」两组 npu 标签，
支持范围完全不同（前者 8 个芯片支持、后者只有 950）。如果按文档存一个布尔值，
回答就必然是错的。所以 `doc_support` 表的主键是
`(doc_id, chip, prototype)`，`api_support` 返回时按 chip 聚合但保留明细。

另一个坑：`npu="950,a3,910b"` 这种**逗号合并标签**。第一版只对整串做映射，
`doc_support` 里出现了脏值 `'950,a3,910b'`。修法是 `expand_chip_value()` 先按
`[,，/;；\s]+` 拆分再逐个映射。

## 3. MCP 层

### 3.1 工具签名：扁平，不要嵌套

这是踩过的最贵的一个坑。用 Pydantic 模型做唯一参数时：

```python
def search(args: SearchRequest) -> dict: ...
# → input_schema: {"properties": {"args": {"$ref": "#/$defs/SearchRequest"}}, "required": ["args"]}
```

`$ref` 嵌套 schema 在 function calling 里容易调错（模型会把参数拍平传，校验失败）。
改成扁平：

```python
def search_docs(query: Annotated[str, Field(description="...")], top_k: int = 6) -> dict: ...
# → {"properties": {"query": {...}, "top_k": {...}}, "required": ["query"]}
```

Pydantic 模型仍然保留在 `models.py` 里做**入参校验**（工具内 `_validated()` 调用）
和测试断言，只是不做工具签名包装。

### 3.2 工具必须不抛异常

模型乱填参数是常态。工具层的约定是：任何情况下都返回 dict，失败也是
`{"error": ..., "details": [{"field": ..., "message": ...}]}`。`_guard()` 兜住
所有意外异常。这样编排循环永远拿到可解析的结果，不会因为一个工具崩掉整轮对话。

分两类错误：

- **业务性失败**（查不到、覆盖范围外）→ 正常 JSON，`found: false` + `hint`，
  让模型知道下一步该换什么工具；
- **协议校验失败**（参数越界）→ MCP 层返回 `is_error=True` + 字段名文本，
  编排循环把 `is_error` 记进 trace 的 `failed_calls`。

### 3.3 传输：streamable-http

stdio 需要父进程建管道，本机沙箱 `_winapi.CreateFile` 被拒（`WinError 5`）。
HTTP 传输还带来两个好处：`serve` 子命令可常驻供多客户端共用；调试时能直接用
curl 打 `/mcp`。`--transport stdio` 保留以兼容标准客户端。

### 3.4 资源与提示模板

除了 tools，还暴露了两类 MCP 能力：

- **resources**：`skill://{name}/{path}` 把 4 个技能文件变成可枚举 URI，
  `chip://facts/{chip}` 把芯片事实变成资源——客户端可以像读文件一样读它们；
- **prompts**：7 个工作流（`lookup_api` / `chip_support` / `write_api_doc` /
  `gen_ut` / `locate_code` / `learn_concept` / `review_markdown`），每个都带具体
  工具参数，别的 MCP 客户端接入后能直接复用这套取证流程。

这些 prompt **不是**在 `server.py` 里手写的，而是从 `asc_agent/workflows.py` 的
单一注册表用 `prompt_specs()` 批量注册——`server.py` 里没有任何 prompt 正文。
好处是 agent 侧和 MCP 侧用同一份定义，改一处两边同时生效；`server.py` 也就不用
每加一个工作流就跟进改一遍。这条约束由测试守着
（`test_mcp_prompts_match_workflow_registry`）。

## 4. Agent 层

### 4.1 提示词只写纪律，不写知识

`prompts.py` 里刻意**不出现**任何具体架构号、dtype 位宽、文档路径。理由：

- 写死事实会随资料更新而腐化；
- 模型会拿记忆里的事实覆盖工具返回的事实（这是检索增强系统最隐蔽的失败模式）。

提示词的六条纪律都对应一次真实事故：

| 纪律 | 事故 |
| --- | --- |
| 先取证再开口 | 早期版本会凭记忆答「Exp 支持 910B」，实际 regbase 版不支持 |
| 架构号只能来自 `chip_facts` | 曾把 `ascend910b1` 答成 1001（应为 2201） |
| 支持性冲突双报 | 文档标签与 CSV 冲突时模型自己挑了一个 |
| 注意 prototype 粒度 | 把两个原型的支持情况混成一句 |
| 检索不到换词，最多两轮 | 曾出现 8 轮反复换词 |
| 区分事实与建议 | 把推断和文档原文混着说 |

### 4.2 循环语义

```
for round in 1..max_rounds(8):
    turn = llm(messages, tools)
    if turn 没有 tool_calls: 收尾，stop_reason = answered
    执行 tool_calls，把结果作为 role=tool 回灌
else:
    追加一条「预算用完，只用已有证据作答」的指令，再调一次（不带 tools）
    stop_reason = max_rounds
```

三个细节：

- **工具结果全量回灌**，不做摘要压缩——chip 名、路径、`supported` 布尔值都是硬事实，
  压掉就会答错；
- **`raw_arguments` 原样回灌**：OpenAI 协议要求 assistant 的 tool_calls 在下一轮
  完全一致，用解析后的 dict 重新序列化可能改变字段顺序导致部分服务端报错；
- **轨迹（Trace）记录每步的 name / arguments / ok / elapsed_ms / chars**，
  `--trace` 可打印，也是调 prompt 的证据。

### 4.3 离线模式

`MockClient` 不是「假装思考」：工具调用是真的（真的查索引、真的读技能），
只有「选哪个工具、怎么措辞」是模板。选工具靠关键词剧本，且**只看用户原问题**——
编排器会把工作流步骤拼进 user 消息，早期版本因此把「Exp 在 910b 上支持吗」
误路由到 `chip_facts`（因为工作流文本里出现了 `__NPU_ARCH__`）。

## 5. 工程坑位记录

| 症状 | 根因 | 修法 |
| --- | --- | --- |
| 服务跑一会儿就 `SQLite objects created in a thread...` | MCP 同步工具跑在 anyio 工作线程池，`lru_cache` 的连接绑定首次使用线程 | `check_same_thread=False` + `threading.Lock` 串行化 |
| CLI 刷满 `HTTP Request: POST /mcp 200 OK` | mcp 2.x 用 **httpx2**（不是 httpx）；且 uvicorn 默认 dictConfig 重配日志级别 | 静音 `httpx2`；`uvicorn.Config(log_config=None)` |
| 收尾时 stderr 打 `ConnectionResetError [WinError 10054]` | Windows proactor 循环在连接被对端关闭时上报 | `loop.set_exception_handler()` 过滤该异常 |
| `--output-dir` 生成了一个 4901 字节的**文件** | 生成器把 `--output-dir` 当输出路径用 | 改用 `--output <file>` |
| `gen_ut --core` 参数不存在 | 生成器用 `--type aiv\|aic` 表达核心类型 | `membase+aiv → aiv` 折算 |
