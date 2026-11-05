"""知识卡片工具：增删改查 + 关系维护 + 一致性体检。

和文档类工具的区别：
* `search_docs` 查的是**官方文档**（外部事实，只读）；
* `card_*` 操作的是**个人知识卡片**（人维护的判断，可写）。

回答时必须区分这两类来源——卡片可能是错的、可能过期，不能当官方口径用。

写操作只碰 `knowledge/cards/*.md`，不碰 asc-devkit（那是只读的上游快照）。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, ValidationError

from asc_agent import config
from asc_agent.knowledge import store
from asc_agent.knowledge.models import (
    RELATION_HINT,
    Card,
    CardStatus,
    CardType,
    Confidence,
    Evidence,
    Scope,
    VerifiedOn,
)

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _split(value: str | None) -> list[str]:
    """逗号/顿号分隔的字符串 → 列表。"""
    if not value:
        return []
    out: list[str] = []
    for part in value.replace("，", ",").replace("、", ",").split(","):
        s = part.strip()
        if s and s not in out:
            out.append(s)
    return out


def _enum(enum_cls, value: str, field: str):
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in enum_cls)
        raise store.CardError(f"{field} 取值非法：{value!r}，可选：{allowed}") from exc


def _err(msg: str) -> dict:
    return {"error": msg}


# ---------------------------------------------------------------------------
# 1. 检索
# ---------------------------------------------------------------------------


def card_search(
    query: Annotated[str, Field(description="自然语言查询，例如「DataCopy 非对齐怎么处理」")],
    architecture: Annotated[
        str, Field(description="按 NpuArch 架构号过滤，如 2201 或 3510，可留空")
    ] = "",
    chip: Annotated[str, Field(description="按具体型号过滤，如 ascend910b4")] = "",
    programming_model: Annotated[
        str, Field(description="按编程模型过滤：c_api/tensor_api/tpipe/simt/simd/scalar/aicpu/...")
    ] = "",
    api: Annotated[str, Field(description="按 API 名过滤，如 Exp")] = "",
    card_type: Annotated[
        str,
        Field(description="按卡片类型过滤：technique/constraint/feature/pitfall/decision"),
    ] = "",
    status: Annotated[str, Field(description="按状态过滤：draft/verified/deprecated")] = "",
    limit: Annotated[int, Field(description="最多返回条数 1-30", ge=1, le=30)] = 8,
    include_deprecated: Annotated[bool, Field(description="是否包含已过时的卡片")] = False,
) -> dict:
    """检索**个人知识卡片**（不是官方文档）。

    卡片记录的是官方文档里没有的经验：用某 API 绕开基础 API 的限制、未文档化的
    约束、踩过的坑、选型结论。回答「我们实测怎么做的」「文档没写的约束」这类问题
    时用它；问官方口径仍要 `search_docs`。
    """
    if not query.strip() and not any([architecture, chip, programming_model, api, card_type]):
        return _err("query 与所有过滤条件都为空，无法检索")
    scope = Scope(
        architecture=_split(architecture),
        chip=_split(chip),
        programming_model=_split(programming_model),
        api=_split(api),
    )
    try:
        if card_type:
            _enum(CardType, card_type, "card_type")
        if status:
            _enum(CardStatus, status, "status")
        hits = store.search_cards(
            query,
            scope=scope,
            card_type=card_type,
            status=status,
            limit=limit,
            include_deprecated=include_deprecated,
        )
    except store.CardError as exc:
        return _err(str(exc))
    return {
        "query": query,
        "count": len(hits),
        "hits": hits,
        "source": "personal_knowledge_card",
        "hint": ""
        if hits
        else "没有匹配的卡片。可以放宽过滤条件，或改用 search_docs 查官方文档。",
    }


# ---------------------------------------------------------------------------
# 2. 读取（含关系图）
# ---------------------------------------------------------------------------


def card_get(
    card_id: Annotated[str, Field(description="卡片 id，例如 datacopy-unaligned-tail")],
) -> dict:
    """读一张卡片的全文 + 关系图（出边与入边）。

    入边是推导出来的：关系只在声明方写一次，读取时补反向，避免双份数据漂移。
    """
    card = store.get_card(card_id.strip())
    if card is None:
        known = [c.id for c in store.list_cards()][:20]
        return {
            "id": card_id,
            "found": False,
            "known": known,
            "hint": "没有这张卡片。用 card_search 查一下，或用 card_save 新建。",
        }
    data = card.model_dump(mode="json")
    data["found"] = True
    data["graph"] = store.graph(card)
    data["source"] = "personal_knowledge_card"
    data["relation_hint"] = RELATION_HINT
    return data


# ---------------------------------------------------------------------------
# 3. 新建 / 更新
# ---------------------------------------------------------------------------


def card_save(
    id: Annotated[
        str,
        Field(description="卡片 id（= 文件名），小写字母/数字/连字符，语义化，如 exp-config-subnormal"),
    ],
    title: Annotated[str | None, Field(description="标题；更新时可省略表示不改")] = None,
    type: Annotated[
        str | None,
        Field(description="technique/constraint/feature/pitfall/decision；更新时可省略"),
    ] = None,
    summary: Annotated[str | None, Field(description="一句话结论，用于列表与检索命中")] = None,
    body: Annotated[
        str | None, Field(description="Markdown 正文：背景 / 做法 / 实测 / 边界")
    ] = None,
    status: Annotated[str | None, Field(description="draft/verified/deprecated")] = None,
    confidence: Annotated[str | None, Field(description="low/medium/high")] = None,
    tags: Annotated[str | None, Field(description="逗号分隔标签，如 对齐,精度")] = None,
    architecture: Annotated[str | None, Field(description="架构号，逗号分隔，如 2201,3510")] = None,
    soc: Annotated[str | None, Field(description="SocVersion，逗号分隔")] = None,
    chip: Annotated[str | None, Field(description="具体型号，逗号分隔")] = None,
    programming_model: Annotated[
        str | None, Field(description="编程模型，逗号分隔，如 c_api,simt")
    ] = None,
    api: Annotated[str | None, Field(description="相关 API，逗号分隔，如 Exp")] = None,
    dtype: Annotated[str | None, Field(description="相关数据类型，逗号分隔")] = None,
    evidence_path: Annotated[str | None, Field(description="证据出处：文档相对路径或 URL")] = None,
    evidence_note: Annotated[str | None, Field(description="这条证据说明了什么")] = None,
    verified_chip: Annotated[str | None, Field(description="实测芯片")] = None,
    verified_cann: Annotated[str | None, Field(description="实测 CANN 版本")] = None,
    create_only: Annotated[bool, Field(description="true 时若已存在则报错，防误覆盖")] = False,
) -> dict:
    """新建或更新一张知识卡片（写入 `knowledge/cards/<id>.md`）。

    **更新语义是"只改传入的字段"**：省略的参数保持原值，传空字符串才是清空。
    新建时必须给 title 和 type。
    """
    card = store.get_card(id.strip())
    creating = card is None
    if creating:
        if not title or not type:
            return _err(f"新建卡片 {id!r} 必须提供 title 与 type")
        try:
            card = Card(id=id.strip(), title=title, type=_enum(CardType, type, "type"))
        except (store.CardError, ValidationError, ValueError) as exc:
            return _err(str(exc))
    else:
        card = card.model_copy(deep=True)

    try:
        if title is not None:
            card.title = title
        if type is not None:
            card.type = _enum(CardType, type, "type")
        if summary is not None:
            card.summary = summary
        if body is not None:
            card.body = body
        if status is not None:
            card.status = _enum(CardStatus, status, "status")
        if confidence is not None:
            card.confidence = _enum(Confidence, confidence, "confidence")
        if tags is not None:
            card.tags = _split(tags)
        for field, value in (
            ("architecture", architecture), ("soc", soc), ("chip", chip),
            ("programming_model", programming_model), ("api", api), ("dtype", dtype),
        ):
            if value is not None:
                setattr(card.scope, field, _split(value))
        if evidence_path is not None or evidence_note is not None:
            card.evidence = [Evidence(kind="doc", path=evidence_path or "", note=evidence_note or "")]
        if verified_chip is not None or verified_cann is not None:
            prev = card.verified_on or VerifiedOn()
            card.verified_on = VerifiedOn(
                chip=verified_chip if verified_chip is not None else prev.chip,
                cann=verified_cann if verified_cann is not None else prev.cann,
                date=prev.date,
            )
        card = Card(**card.model_dump())  # 重新校验一次，保证 id/枚举合法
    except (store.CardError, ValidationError, ValueError) as exc:
        return _err(str(exc))

    try:
        result = store.save_card(card, create_only=create_only)
    except store.CardError as exc:
        return _err(str(exc))
    return {
        "id": card.id,
        "created": result["created"],
        "path": result["path"],
        "status": card.status.value,
        "frontmatter": card.to_frontmatter(),
        "source": "personal_knowledge_card",
        "hint": "卡片已落盘。想让它更容易被检索到，补 summary 与 api 作用域。",
    }


# ---------------------------------------------------------------------------
# 4. 删除
# ---------------------------------------------------------------------------


def card_delete(
    id: Annotated[str, Field(description="要删除的卡片 id")],
    confirm: Annotated[bool, Field(description="必须显式传 true 才会真的删")] = False,
) -> dict:
    """删除一张卡片。默认不删，需要 confirm=true。

    只应对「写错了、重复了」的卡片用。**结论被取代时不要删**——把旧卡标成
    deprecated 并让新卡 `supersedes` 它，否则以后有人照着旧卡写代码。
    """
    if not confirm:
        return {
            "id": id,
            "deleted": False,
            "hint": "未删除。确认要删请传 confirm=true；如果只是结论被取代，"
            "请改为 status=deprecated 并用 card_link(type='supersedes') 关联新卡。",
        }
    try:
        result = store.delete_card(id.strip())
    except store.CardError as exc:
        return _err(str(exc))
    if result["referrers"]:
        result["hint"] = (
            f"以下卡片仍有关系指向 {id}：{', '.join(result['referrers'])}，"
            "请用 card_link 修好或删除这些关系，否则 card_check 会报悬空。"
        )
    return result


# ---------------------------------------------------------------------------
# 5. 关系
# ---------------------------------------------------------------------------


def card_link(
    source: Annotated[str, Field(description="源卡片 id")],
    target: Annotated[str, Field(description="目标卡片 id")],
    type: Annotated[
        str,
        Field(
            description="generalizes/refines/depends_on/supersedes/contradicts/alternative/related"
        ),
    ],
    note: Annotated[str, Field(description="为什么建立这条关系")] = "",
    remove: Annotated[bool, Field(description="true 表示删除这条关系")] = False,
) -> dict:
    """建立或删除两张卡片之间的关系（只写在 source 上，反向读取时推导）。

    关系类型的选择：
    - `refines`：本卡是对方的**特化**（通用做法 → 950 上的具体改法）
    - `generalizes`：本卡比对方更**通用**
    - `depends_on`：本卡成立**需要先满足**对方
    - `supersedes`：本卡**取代**对方（记得把对方标 deprecated）
    - `contradicts`：本卡与对方**结论冲突**，需要人工裁决
    - `alternative`：同一目标的**另一种做法**
    - `related`：弱关联
    """
    try:
        if remove:
            return store.unlink(source.strip(), target.strip(), type)
        return store.link(source.strip(), target.strip(), type, note)
    except store.CardError as exc:
        return _err(str(exc))


# ---------------------------------------------------------------------------
# 6. 体检
# ---------------------------------------------------------------------------


def card_check() -> dict:
    """知识库体检：悬空关系、自环、取代关系未标 deprecated、作用域笔误、
    标了 verified 却没有验证记录等。改完卡片跑一次。"""
    result = store.check()
    result["cards_dir"] = str(config.cards_root())
    return result
