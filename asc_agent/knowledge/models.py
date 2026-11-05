"""知识卡片的数据模型与关系设计。

为什么卡片要单独建模，而不是塞进文档索引
----------------------------------------
官方文档是**外部事实**（谁写的、什么版本、以文档为准）；知识卡片是**个人判断**
（我实测过、我们团队踩过坑、文档没写但硬件是这样）。两者混在一个索引里，
模型会分不清哪句该当权威，所以卡片必须有独立的来源标记、置信度和验证记录。

关系设计
--------
知识天然是网状的，而且不是一种边。这里只保留六种**语义互斥**的关系，
避免出现十条边都叫 "related" 的情况：

* `generalizes` / `refines` —— 抽象层次。通用技巧 ↔ 架构特化版本。
  双向词是为了从任一端都能问「更一般的写法是什么 / 具体到 950 怎么改」。
* `depends_on` —— 前置条件。A 成立需要先满足 B，解释时按链展开。
* `supersedes` —— 新结论取代旧结论。旧卡不能删，要留作历史并标 deprecated，
  否则半年后有人照着旧卡写代码。
* `contradicts` —— 两条经验冲突但都没被证伪。这是**必须暴露给人裁决**的状态，
  agent 不许自己挑一个。
* `alternative` —— 同一目标的另一条路。用于选型对比。
* `related` —— 弱关联，只做延伸阅读。

存储约定：关系**只在声明方写一次**，反向关系在读取时推导（见 `INVERSE`）。
双向各写一份必然漂移，这是卡片系统最容易腐化的地方。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class CardType(str, Enum):
    """卡片记录的是哪类知识。"""

    technique = "technique"    # 用某个 API 实现基础 API 不直接支持的操作
    constraint = "constraint"  # 官方文档没写的约束（对齐、大小、顺序、时序）
    feature = "feature"        # 文档没写的特性/隐含行为
    pitfall = "pitfall"        # 踩过的坑与规避方式
    decision = "decision"      # 选型结论（为什么选 A 不选 B）


class CardStatus(str, Enum):
    draft = "draft"            # 草稿，未验证
    verified = "verified"      # 已验证（有实测记录）
    deprecated = "deprecated"  # 已过时，被别的卡取代


class Confidence(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class RelationType(str, Enum):
    generalizes = "generalizes"
    refines = "refines"
    depends_on = "depends_on"
    supersedes = "supersedes"
    contradicts = "contradicts"
    alternative = "alternative"
    related = "related"


class ProgrammingModel(str, Enum):
    """编程模型/接口层——「某种编程语言特有的知识」就落在这一维。"""

    c_api = "c_api"
    tensor_api = "tensor_api"
    tpipe = "tpipe"
    simt = "simt"
    simd = "simd"
    scalar = "scalar"
    aicpu = "aicpu"
    asc_stdlib = "asc_stdlib"
    inline_asm = "inline_asm"
    host = "host"


# 关系的反向名：读取时推导补全，写入只存一侧
INVERSE: dict[str, str] = {
    RelationType.generalizes.value: RelationType.refines.value,
    RelationType.refines.value: RelationType.generalizes.value,
    RelationType.depends_on.value: "dependency_of",
    "dependency_of": RelationType.depends_on.value,
    RelationType.supersedes.value: "superseded_by",
    "superseded_by": RelationType.supersedes.value,
    # 对称关系：反向名就是自己
    RelationType.contradicts.value: RelationType.contradicts.value,
    RelationType.alternative.value: RelationType.alternative.value,
    RelationType.related.value: RelationType.related.value,
}

# 哪些关系是「有向」的（反向名不同，需要单独推导）
ASYMMETRIC = {
    RelationType.generalizes.value,
    RelationType.refines.value,
    RelationType.depends_on.value,
    RelationType.supersedes.value,
}

RELATION_HINT: dict[str, str] = {
    "generalizes": "本卡比对方更抽象/更通用",
    "refines": "本卡是对方的特化或更细的落地版本",
    "depends_on": "本卡成立需要先满足对方（前置条件）",
    "supersedes": "本卡取代对方（对方应标记 deprecated）",
    "contradicts": "本卡与对方结论冲突，需要人工裁决",
    "alternative": "本卡是同一目标的另一种做法",
    "related": "弱关联，仅供延伸阅读",
}

# 允许作用域写 "all" 表示不限定
ALL = "all"


# ---------------------------------------------------------------------------
# 作用域：一张卡「适用于什么」
# ---------------------------------------------------------------------------


class Scope(BaseModel):
    """四维正交作用域 + 两个可选的精确锚点。

    用户说的「通用的 / 不通用的 / 架构特有 / 型号特有 / 某种编程语言特有」
    在这里被拆成正交维度，而不是塞进一个 `scope: str` 枚举——因为它们是可叠加的：
    例如「SIMT 编程模型下、ascend950 上、fp8 dtype 的非对齐搬运约束」同时命中
    三个维度。用单枚举会丢掉组合信息。
    """

    architecture: list[str] = Field(
        default_factory=list,
        description="NpuArch 架构号，如 2201 / 3510；空或 ['all'] 表示不限架构",
    )
    soc: list[str] = Field(
        default_factory=list, description="SocVersion，如 ASCEND910B / ASCEND950"
    )
    chip: list[str] = Field(
        default_factory=list, description="具体型号，如 ascend910b4 / ascend950pr_9599"
    )
    programming_model: list[str] = Field(
        default_factory=list,
        description="编程模型/接口层：c_api / tensor_api / tpipe / simt / simd / ...",
    )
    api: list[str] = Field(default_factory=list, description="相关 API 名，如 Exp / DataCopyPad")
    dtype: list[str] = Field(default_factory=list, description="相关数据类型")

    @field_validator("architecture", "soc", "chip", "programming_model", "api", "dtype")
    @classmethod
    def _strip(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for item in v or []:
            s = str(item).strip()
            if s and s.lower() not in {x.lower() for x in out}:
                out.append(s)
        return out

    def is_universal(self) -> bool:
        """六个维度全空或全为 all → 通用知识。"""
        for dim in (
            self.architecture, self.soc, self.chip,
            self.programming_model, self.api, self.dtype,
        ):
            if dim and not all(d.lower() == ALL for d in dim):
                return False
        return True

    def matches(self, other: "Scope") -> bool:
        """本卡是否覆盖 `other` 这个查询范围。

        规则：未限定的维度视为「全匹配」；双方都限定时要求有交集。
        卡片比查询更具体时**不算匹配**——查「通用做法」不该返回某型号专有的卡。
        """
        for name in ("architecture", "soc", "chip", "programming_model", "api", "dtype"):
            mine = set(_norm(getattr(self, name)))
            theirs = set(_norm(getattr(other, name)))
            if not theirs:
                continue
            if not mine:
                # 卡片不限该维度 → 覆盖
                continue
            if not (mine & theirs):
                return False
        return True


def _norm(items: list[str]) -> list[str]:
    out = []
    for i in items:
        s = str(i).strip().lower()
        if s and s != ALL:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# 关系与证据
# ---------------------------------------------------------------------------


class Relation(BaseModel):
    type: RelationType
    target: str = Field(description="目标卡片 id")
    note: str = Field(default="", description="为什么建立这条关系")


class Evidence(BaseModel):
    """证据：官方文档出处，或实测记录。"""

    kind: Literal["doc", "test", "code", "external"] = "doc"
    path: str = Field(default="", description="文档/源码相对路径，或外部 URL")
    note: str = Field(default="", description="这条证据说明了什么")


class VerifiedOn(BaseModel):
    """验证记录——没有它就无法判断卡片是否过期。"""

    chip: str = ""
    cann: str = ""
    date: str = ""


# ---------------------------------------------------------------------------
# 卡片
# ---------------------------------------------------------------------------

CARD_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


class Card(BaseModel):
    id: str = Field(description="稳定 slug，小写字母/数字/连字符，同时是文件名")
    title: str
    type: CardType
    status: CardStatus = CardStatus.draft
    confidence: Confidence = Confidence.medium
    summary: str = Field(default="", description="一句话结论，用于列表和检索命中摘要")
    scope: Scope = Field(default_factory=Scope)
    tags: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    verified_on: VerifiedOn | None = None
    relations: list[Relation] = Field(default_factory=list)
    author: str = ""
    created: str = ""
    updated: str = ""
    body: str = Field(default="", description="Markdown 正文：背景 / 做法 / 实测 / 边界")

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        import re

        v = (v or "").strip()
        if not re.fullmatch(CARD_ID_PATTERN, v):
            raise ValueError(
                f"卡片 id 必须匹配 {CARD_ID_PATTERN}，收到 {v!r}"
                "（小写字母、数字、连字符；同时作为文件名）"
            )
        return v

    def to_frontmatter(self) -> dict:
        """序列化成 YAML frontmatter（去掉正文与空值，保持文件可读）。"""
        data: dict = {
            "id": self.id,
            "title": self.title,
            "type": self.type.value,
            "status": self.status.value,
            "confidence": self.confidence.value,
            "summary": self.summary,
        }
        scope = {
            k: v
            for k, v in self.scope.model_dump().items()
            if v and not (len(v) == 1 and str(v[0]).lower() == ALL)
        }
        if scope:
            data["scope"] = scope
        if self.tags:
            data["tags"] = self.tags
        if self.evidence:
            data["evidence"] = [
                {k: v for k, v in e.model_dump().items() if v} for e in self.evidence
            ]
        if self.verified_on and any(self.verified_on.model_dump().values()):
            data["verified_on"] = {
                k: v for k, v in self.verified_on.model_dump().items() if v
            }
        if self.relations:
            data["relations"] = [
                {"type": r.type.value, "target": r.target, **({"note": r.note} if r.note else {})}
                for r in self.relations
            ]
        for key in ("author", "created", "updated"):
            if getattr(self, key):
                data[key] = getattr(self, key)
        return data

    def brief(self) -> dict:
        """列表用摘要（不含正文）。"""
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type.value,
            "status": self.status.value,
            "confidence": self.confidence.value,
            "summary": self.summary,
            "scope": {k: v for k, v in self.scope.model_dump().items() if v},
            "tags": self.tags,
            "updated": self.updated,
        }
