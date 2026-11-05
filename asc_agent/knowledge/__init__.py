"""个人知识卡片子系统。

和 `indexing` 的区别：`indexing` 索引的是**外部官方文档**（只读、可重建）；
这里的卡片是**人手工维护的判断**（可写、要进版本库、有置信度与验证记录）。
两者在回答里必须分开标注来源。
"""

from asc_agent.knowledge.models import (
    Card,
    CardStatus,
    CardType,
    Confidence,
    Evidence,
    Relation,
    RelationType,
    Scope,
    VerifiedOn,
)

__all__ = [
    "Card",
    "CardStatus",
    "CardType",
    "Confidence",
    "Evidence",
    "Relation",
    "RelationType",
    "Scope",
    "VerifiedOn",
]
