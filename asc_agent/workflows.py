"""工作流注册表——**唯一事实源**。

为什么要单独一个模块，而不是放进 `agent/prompts.py`：

* `agent` 层要用它（把取证步骤注入任务提示词）；
* `mcp_server` 层也要用它（把同一批工作流注册成标准 MCP prompt）。

如果注册表放在 `agent/` 里，`mcp_server` 就得反过来依赖 `agent`，层次是倒的。
放在包顶层后，两边都只依赖它，谁也不依赖谁。

三个消费者共用同一份定义，所以新增工作流只改这一个文件：

1. agent 编排循环 → `agent.prompts.build_task_prompt()`
2. MCP server   → `mcp_server.server.build_server()` 逐个 `add_prompt()`
3. CLI / 文档   → `list_workflows()`
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Param:
    """一个工作流参数。

    同一份定义服务两个消费者，所以除了名字还要给两样东西：

    * `generic` —— 未提供实际值时，在步骤文本里显示的占位说法；
    * `default` —— MCP prompt 可选参数的默认值。
    """

    name: str
    description: str
    required: bool = False
    default: str = ""
    generic: str = ""


@dataclass(frozen=True)
class Workflow:
    key: str
    title: str
    when: str
    steps: str
    deliver: str
    params: tuple[Param, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# 工作流定义
#
# 步骤模板里的 {xxx} 由 Param 名替换：有实际值填值，没有填 generic 占位。
# 模板自己负责加引号/反引号，渲染器只做纯文本替换，避免引号嵌套出错。
# ---------------------------------------------------------------------------

WORKFLOWS: dict[str, Workflow] = {
    "lookup_api": Workflow(
        key="lookup_api",
        title="查 API 用法",
        when="用户问某个 AscendC API 怎么用、参数是什么、头文件在哪、有没有示例",
        steps="""\
1. `api_card(name="{api}", category="{category}")` —— 拿原型、参数表、头文件、支持矩阵；
2. `search_docs(query="{api} 调用示例", kind="example")` —— 找可运行样例；
3. `api_support(api="{api}", chip="{chip}")` —— 核对支持性（chip 留空表示列全部芯片）；
4. `find_symbol(name="{api}", scope="include")` —— 看头文件里的真实声明；
5. 同名 API 分属不同目录时，用 `category` 消歧（如 memory_vector_compute / reg_vector_compute）。""",
        deliver="一句话结论 → 函数原型（cpp 代码块）→ 参数说明表 → 支持芯片 → "
        "头文件路径 → 出处（每段都标 path）。不确定的地方标「文档未明确」。",
        params=(
            Param("api", "API 名称，例如 Exp", required=True, generic="<API 名>"),
            Param("chip", "芯片名，留空表示全部芯片", default="", generic="<芯片名>"),
            Param("category", "同名 API 消歧用目录关键字，可留空", default="", generic=""),
        ),
    ),
    "chip_support": Workflow(
        key="chip_support",
        title="查芯片支持性",
        when="用户问某 API / 特性在哪个芯片上支持，或某芯片能做什么",
        steps="""\
1. `api_support(api="{api}", chip="{chip}")` —— 同时给出文档 npu 标签与仓库 CSV 两路证据；
2. 若问的是「某芯片支持哪些」，用 `chip_apis(chip="{chip}", group="")`；
3. 涉及架构号 / SocVersion / __NPU_ARCH__ / dtype 位宽时，必须 `chip_facts(chip="{chip}")`，
   不许凭记忆写架构号；
4. 注意 prototype 粒度：同一 API 的不同重载支持情况可能不同，要分开说；
5. 结论里明确区分「CSV 有记录」与「只有文档标签」两类证据。""",
        deliver="先给结论（支持 / 不支持 / 部分支持），再按原型或芯片列证据，"
        "最后标出来源（文档标签 or CSV）与资料缺口。",
        params=(
            Param("api", "API 名称；只问芯片能力时可留空", default="", generic="<API 名>"),
            Param("chip", "芯片名，例如 ascend910b", required=True, generic="<芯片名>"),
        ),
    ),
    "write_api_doc": Workflow(
        key="write_api_doc",
        title="写 / 改 API 文档",
        when="用户要新写一篇 API 文档，或补产品支持情况、约束说明等章节",
        steps="""\
1. `read_skill(skill="asc-doc-checker")` 读规范；细节再读
   `read_skill(skill="asc-doc-checker", file="references/writing_specs.md")`；
2. `search_docs(query="{api} 接口说明")` 找 2~3 篇同类 API 文档做结构参照；
3. `api_card(name="{api}")` 取真实原型与参数，禁止手编签名；
4. 按固定章节顺序写：产品支持情况 → 功能说明 → 函数原型 → 参数说明 →
   数据类型（可选）→ 返回值说明 → 约束说明 → 调用示例；
5. 芯片标签用 `<!-- npu="950" id1 -->` 形式，组内顺序 950→A3→910b→910→310p→310b→x90→9030；
6. 写完把文件落到 docs/ 下，再 `check_markdown(path="{path}")` 自检并把问题逐条修掉。""",
        deliver="按标准章节顺序产出的文档 + `check_markdown` 的通过情况；"
        "问题要落到「第 N 行 `xxx` 应改为 `yyy`」级别。",
        params=(
            Param("api", "API 名称，例如 Exp", required=True, generic="<API 名>"),
            Param("path", "文档落盘路径，用于自检", default="", generic="<文档路径>"),
        ),
    ),
    "gen_ut": Workflow(
        key="gen_ut",
        title="生成单元测试",
        when="用户要某个 API 的 UT 骨架",
        steps="""\
1. `read_skill(skill="asc-api-ut-gen")` 读参数顺序与架构约束
   （regbase / SIMT 仅 ascend950pr_9599 支持）；
2. `chip_facts(chip="{chip}")` 确认芯片调用名合法；
3. `gen_ut(api="{api}", api_type="{api_type}", chip="{chip}")` 产出 gtest 骨架；
4. `read_doc(path="{path}")` 读生成的文件，判断是否需要补 kernel_params；
5. 若生成器报错（例如 AIV 模板只覆盖已验证的二元 API），把**原文**错误照抄给用户，
   再用 `find_symbol(name="{api}", scope="impl")` 与 `find_examples(keyword="{api}")`
   给出「先读哪个 impl、抄哪个样例」的替代路径。""",
        deliver="生成的文件路径 + 关键片段 + 是否可直接编译；失败时给原文报错与下一步。",
        params=(
            Param("api", "API 名称，例如 Add", required=True, generic="<API 名>"),
            Param("chip", "芯片调用名", default="ascend910b1", generic="<芯片名>"),
            Param(
                "api_type",
                "API 类型：membase/regbase/adv/c/simt/utils 或 aiv/aic",
                default="membase",
                generic="<API 类型>",
            ),
            Param("path", "生成文件路径，用于复核", default="", generic="<生成文件路径>"),
        ),
    ),
    "locate_code": Workflow(
        key="locate_code",
        title="定位实现 / 找样例",
        when="用户想知道某能力在头文件或实现里怎么定义，或想找一个能跑的样例工程",
        steps="""\
1. `find_symbol(name="{symbol}", scope="all")` —— 定位定义（`definition: true` 才是定义）；
2. `find_examples(keyword="{keyword}")` —— 找样例工程，再 `read_doc` 读它的 README；
3. `search_docs(query="{keyword}", kind="guide")` —— 补概念性说明。""",
        deliver="文件路径 + 行号 + 关键代码片段，并说明该样例覆盖什么场景。",
        params=(
            Param("symbol", "符号名，例如 DataCopy", default="", generic="<符号名>"),
            Param("keyword", "样例场景关键字，例如 double buffer", default="", generic="<场景关键字>"),
        ),
    ),
    "learn_concept": Workflow(
        key="learn_concept",
        title="概念 / 入门答疑",
        when="用户问「什么是 double buffer」「怎么入门写算子」这类需要解释的问题",
        steps="""\
1. `search_docs(query="{question}", kind="guide")` —— 优先 getting_started / overview；
2. `search_docs(query="{question}", kind="api_doc")` —— 补 API 层面的依据；
3. 需要时 `read_doc(path="{path}")` 精读某节，`fetch_url` 取官方在线文档补充。""",
        deliver="先用两三句话讲清概念，再给「在 AscendC 里对应什么 API / 什么机制」，"
        "最后列延伸阅读路径。",
        params=(
            Param("question", "用户原问题", default="", generic="<用户问题>"),
            Param("path", "要精读的文档路径，可留空", default="", generic="<文档路径>"),
        ),
    ),
    "review_markdown": Workflow(
        key="review_markdown",
        title="文档规范检查",
        when="用户要检查一篇 Markdown 是否符合 asc-doc-checker 规范",
        steps="""\
1. `check_markdown(path="{path}")` —— 跑机器可判定的规则（标点/标题/代码块/图注/
   芯片标签/章节顺序/术语/dtype 写法/废弃接口）；
2. 对机器判不了的部分（表述是否准确、图是否符合意图），
   `read_skill(skill="asc-doc-checker")` 读规范后人工判断；
3. 输出：结论（通过 / 不通过）→ 按规则分组的问题清单（带行号与原文片段）→ 逐条修改建议；
4. 不要只说「有空格问题」，要给出「第 N 行 `xxx` 应改为 `yyy`」。""",
        deliver="结论 + 按规则分组的问题清单（含行号与原文）+ 逐条可执行的修改建议。",
        params=(
            Param("path", "待检查的 Markdown 路径", required=True, generic="<文档路径>"),
        ),
    ),
}

# 客户端/用户可能用别的叫法，统一收敛到 key（含历史 MCP prompt 名）
WORKFLOW_ALIASES: dict[str, str] = {
    "api": "lookup_api",
    "usage": "lookup_api",
    "support": "chip_support",
    "chip": "chip_support",
    "chip_capability": "chip_support",
    "doc": "write_api_doc",
    "ut": "gen_ut",
    "gen_ut_for_api": "gen_ut",
    "code": "locate_code",
    "find": "locate_code",
    "concept": "learn_concept",
    "review": "review_markdown",
    "lint": "review_markdown",
}


def resolve_workflow(name: str) -> str:
    """把用户输入收敛成工作流 key；无法识别时返回空串（调用方负责提示）。"""
    if not name:
        return ""
    key = name.strip().lower()
    if key in WORKFLOWS:
        return key
    return WORKFLOW_ALIASES.get(key, "")


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _values(wf: Workflow, supplied: dict[str, str] | None) -> dict[str, str]:
    """合成占位符替换表：调用时给的参数 > 参数默认值 > generic 占位。"""
    supplied = supplied or {}
    out: dict[str, str] = {}
    for p in wf.params:
        value = supplied.get(p.name, p.default) or ""
        out[p.name] = value.strip() or p.generic
    return out


def render_steps(key: str, supplied: dict[str, str] | None = None) -> str:
    """渲染取证步骤（agent 侧注入、MCP 侧输出都用它）。"""
    wf = WORKFLOWS[key]
    return wf.steps.format(**_values(wf, supplied))


def render_prompt(key: str, supplied: dict[str, str] | None = None) -> str:
    """渲染成一条可直接执行的 MCP prompt 正文。"""
    wf = WORKFLOWS[key]
    return (
        f"请按下面顺序完成任务：{wf.title}\n\n"
        f"{render_steps(key, supplied)}\n\n"
        f"输出要求：{wf.deliver}\n"
    )


def prompt_specs() -> list[dict]:
    """给 MCP server 的注册清单：新增工作流不需要动 server 代码。"""
    specs: list[dict] = []
    for key, wf in WORKFLOWS.items():
        specs.append(
            {
                "name": key,
                "title": wf.title,
                "description": wf.when,
                "arguments": [
                    {"name": p.name, "description": p.description, "required": p.required}
                    for p in wf.params
                ],
                # 用默认参数把 key 绑死，避免闭包捕获到循环变量
                "render": (lambda k: lambda **kwargs: render_prompt(k, kwargs))(key),
            }
        )
    return specs


def list_workflows() -> list[dict[str, str]]:
    return [
        {
            "key": wf.key,
            "title": wf.title,
            "when": wf.when,
            "deliver": wf.deliver,
            "params": ", ".join(f"{p.name}{'' if p.required else '?'}" for p in wf.params),
        }
        for wf in WORKFLOWS.values()
    ]
