"""工具契约：Pydantic 参数模型与结果模型。

这一层是**唯一**的契约定义处：

* MCP server 的工具签名由它推导；
* agent 侧 function-calling 的 JSON Schema 由它生成；
* 单元测试直接构造这些模型做断言。

这样「工具含义」只写一遍，避免 server / prompt / 测试三处漂移。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class DocKind(str, Enum):
    """资料条目类型。"""

    api_doc = "api_doc"
    example = "example"
    skill = "skill"
    guide = "guide"
    doc = "doc"
    any = "any"


class ApiType(str, Enum):
    """API 类型。

    同时接受两套命名，避免用户按技能文档写 `membase` 却被生成器的
    `aiv/aic/c_api/reg` 拒绝（反之亦然）——`tools.gen_ut` 里做折算。
    """

    # 技能文档里的叫法
    membase = "membase"
    regbase = "regbase"
    adv = "adv"
    c = "c"
    simt = "simt"
    utils = "utils"
    # 生成器 CLI 里的叫法
    aiv = "aiv"
    aic = "aic"
    c_api = "c_api"
    reg = "reg"


class CoreType(str, Enum):
    aic = "aic"
    aiv = "aiv"


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    """自然语言检索资料库。"""

    query: str = Field(description="用户的自然语言问题，例如「怎么做 fp32 转 bf16」")
    top_k: int = Field(default=6, ge=1, le=20, description="返回片段数")
    kind: DocKind = Field(default=DocKind.any, description="限定条目类型，any 表示不过滤")
    category: str | None = Field(
        default=None, description="按目录关键字过滤，例如 SIMD-API/basic_api 或 adv_api"
    )
    chip: str | None = Field(
        default=None, description="限定芯片，例如 ascend910b、ascend950；只有 API 文档会被过滤"
    )


class ApiCardRequest(BaseModel):
    """取单个 API 的「卡片」：原型、参数、头文件、支持矩阵。"""

    name: str = Field(description="API 名称，大小写不敏感，例如 Exp、Cast、Add")
    category: str | None = Field(
        default=None, description="同名 API 分布在多个目录时用它消歧，例如 memory_vector_compute"
    )


class SupportRequest(BaseModel):
    """查询某 API 在某芯片上是否支持。"""

    api: str = Field(description="API 名称，例如 Exp")
    chip: str | None = Field(
        default=None, description="芯片名，例如 ascend910b；留空则返回所有芯片"
    )


class ChipApisRequest(BaseModel):
    """反查某芯片支持哪些 API（走仓库自带的支持性 CSV）。"""

    chip: str = Field(description="芯片名，例如 ascend910b、ascend310b")
    group: str | None = Field(
        default=None, description="API 分组前缀，例如 adv_api/math_compute、c_api/reg"
    )
    only_supported: bool = Field(default=True, description="只列 status=yes 的条目")
    limit: int = Field(default=40, ge=1, le=500, description="最多返回条数")


class ChipFactsRequest(BaseModel):
    """查询芯片架构事实（NpuArch / SocVersion / __NPU_ARCH__ / dtype 支持）。"""

    chip: str | None = Field(
        default=None, description="芯片调用名，例如 ascend910b1、ascend950pr_9599；留空列全部"
    )
    dtype: str | None = Field(
        default=None, description="同时查询某数据类型的位宽/对齐/取值范围，例如 fp8_e4m3fn_t"
    )


class ReadDocRequest(BaseModel):
    """按路径读资料原文（可只取某个章节）。"""

    path: str = Field(
        description="相对 asc-devkit 的路径，例如 docs/zh/api/SIMD-API/basic_api/.../Exp.md"
    )
    section: str | None = Field(
        default=None, description="只取标题包含该关键字的章节，例如 约束说明"
    )
    max_chars: int = Field(default=4000, ge=200, le=20000, description="正文最大字符数")


class SkillRequest(BaseModel):
    """读取 .agent/skills 下的技能说明或参考文档。"""

    skill: str = Field(
        description="技能名：asc-npu-arch / asc-ar-dev / asc-api-ut-gen / asc-doc-checker"
    )
    file: str = Field(
        default="SKILL.md",
        description="技能内相对路径，例如 SKILL.md、references/08_api_lookup.md",
    )


class MarkdownCheckRequest(BaseModel):
    """按 asc-doc-checker 的规范检查一篇 Markdown。"""

    path: str = Field(description="待检查文档的相对路径")
    max_issues: int = Field(default=40, ge=1, le=200, description="最多返回的问题条数")


class SymbolSearchRequest(BaseModel):
    """在头文件/实现里定位符号定义（补资料库检索的盲区）。"""

    name: str = Field(description="符号名，例如 Exp、DataCopy、SetFlag")
    scope: Literal["all", "include", "impl", "examples"] = Field(
        default="all", description="搜索范围"
    )
    limit: int = Field(default=10, ge=1, le=50, description="最多返回的命中数")


class ExampleSearchRequest(BaseModel):
    """按关键字找可运行的样例工程。"""

    keyword: str = Field(description="关键字，例如 cast、matmul、double buffer")
    limit: int = Field(default=8, ge=1, le=30, description="最多返回条数")


class UTGenRequest(BaseModel):
    """调用仓库自带的 UT 生成器产出测试骨架（只落盘，不编译）。"""

    api: str = Field(description="API 名称，例如 Add")
    api_type: ApiType = Field(description="API 类型")
    chip: str = Field(description="芯片调用名，例如 ascend910b1、ascend950pr_9599")
    core: CoreType | None = Field(default=None, description="aic 或 aiv，部分类型需要")
    out_dir: str = Field(default="", description="输出目录（相对仓库根）；留空用默认目录")


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------


class Hit(BaseModel):
    path: str
    title: str = ""
    heading: str = ""
    kind: str = ""
    category: str = ""
    line: int = 0
    score: float = 0.0
    snippet: str = ""


class SearchResult(BaseModel):
    query: str
    count: int
    hits: list[Hit] = Field(default_factory=list)
    hint: str = ""


class SupportRow(BaseModel):
    chip: str
    prototype: str = ""
    product: str = ""
    supported: bool = False


class ApiCard(BaseModel):
    name: str
    path: str
    category: str = ""
    headers: list[str] = Field(default_factory=list)
    chips: list[str] = Field(default_factory=list)
    summary: str = ""
    support: list[SupportRow] = Field(default_factory=list)
    sections: dict[str, str] = Field(default_factory=dict)


class SupportAnswer(BaseModel):
    api: str
    found: bool = False
    by_chip: dict[str, list[SupportRow]] = Field(default_factory=dict)
    csv: list[dict] = Field(default_factory=list)
    verdict: str = ""


class CheckIssue(BaseModel):
    rule: str
    line: int = 0
    message: str
    snippet: str = ""
    severity: str = Field(default="warn", description="error 表示硬性违规，warn 表示风格问题")


class CheckReport(BaseModel):
    path: str
    passed: int = 0
    total_rules: int = 0
    issues: list[CheckIssue] = Field(default_factory=list)
    checked_rules: list[str] = Field(default_factory=list)
    error: str = ""
