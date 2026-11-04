# 常见工作流设计

工作流是**注入提示词的取证步骤**，不是代码分支。这么设计的理由：这类任务的差别
体现在「先查什么、后查什么、冲突怎么办」，用自然语言表达比用 if/else 更能吸收
边界情况；而且新增一类任务只需在 `prompts.py: WORKFLOWS` 加一段文本，不用动编排器。

调用方式：`python -m asc_agent ask "..." --workflow <key>`；不指定时由模型自己判断。

---

## W1 `lookup_api` — 查 API 用法

**用户会这样问**：「Exp 怎么用」「Cast 的参数是什么」「DataCopy 要 include 什么头文件」

**取证顺序**

1. `api_card(name=API名)` — 拿函数原型、参数表、头文件、支持矩阵；
2. `search_docs(query="<API名> 调用示例", kind="example")` — 找可运行样例；
3. `find_symbol(name=API名, scope="include")` — 确认头文件里的真实声明；
4. 同名 API 分布在不同目录时用 `category` 消歧。

**为什么这个顺序**：卡片是结构化的、最省 token；样例和源码声明是交叉验证。
`Exp` 在 regbase / membase / adv 三个目录各有一篇文档，只查一篇会漏掉实现形态差异，
所以 `api_card` 会在 `alternatives` 里列出其余同名文档。

**交付物**：函数原型（cpp 代码块）+ 参数表 + 头文件路径 + 支持芯片 + 出处路径。

---

## W2 `chip_support` — 查芯片支持性

**用户会这样问**：「Exp 在 910b 上支持吗」「950 能用 fp8 吗」「310b 支持哪些高阶 API」

**取证顺序**

1. `api_support(api=..., chip=...)` — 它**同时**返回文档 `npu` 标签与仓库 CSV 两路证据；
2. 问「某芯片支持哪些」→ `chip_apis(chip=..., group=...)`；
3. 涉及架构号 / SocVersion / dtype 位宽 → `chip_facts()`，禁止凭记忆；
4. 注意 prototype 粒度，同一 API 的不同重载要分开说。

**最容易答错的三个点**

- **CSV 只覆盖 `ascend910b` / `ascend310b` 两代**。问 950 时 `chip_apis` 会返回
  `found: false` 和覆盖范围说明，正确做法是转 `api_support` 看文档标签，而不是答
  「不支持」。
- **同名文档支持性不同**。`Exp` 的 regbase 版 910B 不支持，membase 版支持。
  工具会按文档分别列出，回答也必须按文档说。
- **假冲突**。CSV 里 `Exp` 在 910B 下同时有 yes 和 no 两行，因为它们对应**两篇不同文档**。
  `api_support` 的冲突判定是**逐文档**比对（CSV 行 ↔ 同路径文档的标签），
  所以这种情况不会误报冲突。

**交付物**：一句话结论（支持/不支持/部分支持）→ 按文档列证据 → 标出来源。

---

## W3 `write_api_doc` — 写 / 改 API 文档

**用户会这样问**：「帮我写个 Foo 的 API 文档」「这篇文档帮我按规范检查一下」

**取证顺序**

1. `read_skill(skill="asc-doc-checker")` 读 14 条规范；
   细节再读 `read_skill(skill="asc-doc-checker", file="references/writing_specs.md")`；
2. `search_docs` 找 2~3 篇同类文档做结构参照；
3. `api_card` 取真实原型与参数（**禁止手编签名**）；
4. 写完后 `check_markdown(path=...)` 自检，按 issues 逐条修。

**规范被仪器化的部分**（`lint.py`，10 组规则，全部跳过代码块内部）：

| 规则 | 检查内容 |
| --- | --- |
| R1 | 中英文/中文与数字之间不加空格；中文语境不用英文标点 |
| R2 | 标题 `# ` 空格、层级不跳级、末尾无标点、不手工编号 |
| R3 | 代码块声明语言、上下留空行 |
| R4/R5 | 图注在图片上方且格式 `**图 n**  标题`；图编号连续；路径用正斜杠 |
| R8 | `npu` 标签取值合法、id 从 id1 递增不重复、组内顺序 950→A3→910b→910→310p→310b→x90→9030、**块内产品名与标签一致**；禁止 `Atlas A2/A3` 合并写法 |
| R9 | dtype 用标准名（禁止 `Int8`/`S8`/`BF16` 等变体） |
| R10 | API 文档章节顺序：产品支持情况→功能说明→函数原型→参数说明→(数据类型)→返回值说明→约束说明→调用示例 |
| R11 | 术语：`L0A/L0B/L0C/L1` 带 `Buffer` 后缀、`AI Core` 不写 `AICore`、通路用 `->` 不用 `→` |
| R13 | 必须写「头文件路径为：」+ 单反引号包裹 |
| R14 | 对照 `interface_change_description.md` 检查废弃接口名 |

**交付物**：按标准章节顺序产出的文档 + `check_markdown` 通过情况。

---

## W4 `gen_ut` — 生成单元测试

**用户会这样问**：「给 Add 生成个 UT」「Exp 的单元测试怎么写」

**取证顺序**

1. `read_skill(skill="asc-api-ut-gen")` 读参数顺序与架构约束
   （regbase / SIMT 仅 `ascend950pr_9599` 支持）；
2. `gen_ut(api=..., api_type=..., chip=...)` 产出 gtest 骨架；
3. `read_doc` 读生成的文件，判断是否需要补 `kernel_params`；
4. 失败时把生成器原文错误转述，再给替代路径。

**生成器的真实限制**（实测）

```
$ python ut_generator_cli.py --type aiv --api Exp --chip ascend910b1
代码生成失败: AIVUTGenerator cannot infer an executable AIV UT signature for 'Exp'.
Generic AIV skeleton generation is limited to verified binary APIs.
Read the target impl and same-category UT first, then pass kernel_params.aiv_template
explicitly or write an API-specific UT.
```

这不是 bug 而是设计：通用模板只对已验证二元签名安全。此时正确的做法是
`find_symbol(name="Exp", scope="impl")` + `find_examples(keyword="exp")`
找到同类实现参照，再手写专属 UT——工具会把这句话告诉用户。

**交付物**：生成文件路径 + 关键片段 + 能否直接编译；失败时给原文报错与下一步。

---

## W5 `locate_code` — 定位实现 / 找样例

**用户会这样问**：「Exp 在头文件里怎么声明的」「有没有 matmul 的样例」

**取证顺序**

1. `find_symbol(name=符号名, scope="include"|"impl"|"examples")` — 定位定义（标记 `definition: true`）；
2. `find_examples(keyword=场景词)` — 找样例工程，再 `read_doc` 读它的 README；
3. `search_docs(kind="guide")` — 补概念性说明。

**为什么要单独一条**：Markdown 索引覆盖不到源码。`DataCopy` 这类 API 的文档文件名
不是 `DataCopy.md`（而是 `DataCopy_GMToUB_continuous.md` 等十几个），资料检索只能
给近似结果，真正的签名必须看 `include/`。

**交付物**：文件路径 + 行号 + 关键片段 + 样例覆盖的场景说明。

---

## W6 `learn_concept` — 概念 / 入门答疑

**用户会这样问**：「double buffer 是什么」「怎么入门写算子」「核间同步怎么理解」

**取证顺序**

1. `search_docs(kind="guide")` — 优先 `getting_started` / `overview`；
2. `search_docs(kind="api_doc")` — 补 API 层面的依据；
3. 需要时 `read_doc` 精读某节，或 `fetch_url` 取官方在线文档补充。

**排序上做了什么**：`_intent_prior()` 把「入门/教程」路由到 `getting_started`，
「怎么/如何」路由到 `overview`；`_soft_negative()` 把 `asc_doc_contributing`
（文档编写规范）在入门类查询里降权——否则「入门教程」会被「入门教程编写规范」
这个章节抢到第一。

**交付物**：两三句话讲清概念 → 在 AscendC 里对应什么 API/机制 → 延伸阅读路径。

---

## 工作流之外：模型自己怎么选

不指定 `--workflow` 时，系统提示词里列了 6 个工作流的触发条件，由模型判断。
实践中的典型路由：

| 问句 | 实际路由 |
| --- | --- |
| 「Exp 在 910b 上支持吗」 | `chip_support` → `api_support` |
| 「Cast 怎么用」 | `lookup_api` → `api_card` |
| 「Ascend 950 的架构号是多少」 | `chip_support` → `chip_facts` |
| 「UT 怎么生成」 | `gen_ut` → `read_skill` |
| 「有没有 cast 的样例」 | `locate_code` → `find_examples` |

真实演示输出见 [`demo.md`](demo.md)。
