"""卡片的文件存储：Markdown + YAML frontmatter。

为什么用文件而不是数据库
------------------------
这些知识是**人手写手改**的。文件的好处是：能用编辑器直接改、能 diff、能进
git、能 code review、能 PR。数据库对「人维护」这件事是负担（改一条要走工具）。
所以真相在 `knowledge/cards/*.md`，本模块只做读写与检索，不引入第二份状态。

副作用是检索得自己扫目录，但卡片量级是几十到几百，全量加载只要毫秒级。
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import date
from pathlib import Path

import yaml

from asc_agent import config
from asc_agent.indexing.tokenizer import highlight_terms
from asc_agent.knowledge.models import (
    ASYMMETRIC,
    INVERSE,
    Card,
    Relation,
    RelationType,
    Scope,
)

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


class CardError(ValueError):
    """卡片操作失败（校验不过、目标不存在等），由工具层转成结构化错误。"""


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------


def _root() -> Path:
    root = config.cards_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(card_id: str) -> Path:
    return _root() / f"{card_id}.md"


def parse_card_text(text: str, fallback_id: str = "") -> Card:
    """把文件内容解析成 Card。frontmatter 缺失或不是映射时明确报错。"""
    m = FRONTMATTER.match(text.lstrip("\ufeff"))
    if not m:
        raise CardError("缺少 YAML frontmatter（文件必须以 --- 开头、以 --- 结束）")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise CardError(f"frontmatter YAML 解析失败：{exc}") from exc
    if not isinstance(meta, dict):
        raise CardError("frontmatter 必须是键值映射")
    meta.setdefault("id", fallback_id)
    meta["body"] = m.group(2).strip()
    try:
        return Card(**meta)
    except Exception as exc:  # pydantic ValidationError
        raise CardError(f"卡片字段校验失败：{exc}") from exc


def render_card_text(card: Card) -> str:
    meta = card.to_frontmatter()
    head = yaml.safe_dump(
        meta, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100
    )
    body = card.body.strip() or "## 背景\n\n（待补充）\n"
    return f"---\n{head}---\n\n{body}\n"


# 简单 mtime 缓存：文件被手工编辑后自动失效
_cache: dict[str, tuple[float, Card]] = {}


def list_cards(reload: bool = False) -> list[Card]:
    """读全部卡片，按 updated 倒序。单张解析失败不影响其它卡片。"""
    cards: list[Card] = []
    for p in sorted(_root().glob("*.md")):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        key = str(p)
        cached = _cache.get(key)
        if cached and cached[0] == mtime and not reload:
            cards.append(cached[1])
            continue
        try:
            card = parse_card_text(p.read_text(encoding="utf-8"), fallback_id=p.stem)
        except (CardError, OSError):
            continue  # 坏文件不阻塞整体，check() 会单独报
        # 文件名是**寻址依据**：frontmatter 里 id 写成别的值时，以文件名为准，
        # 否则 save 会写到另一个文件、delete 也删不到（check() 会把不一致报出来）。
        card = _align_id(card, p.stem)
        _cache[key] = (mtime, card)
        cards.append(card)
    cards.sort(key=lambda c: (c.updated or "", c.id), reverse=True)
    return cards


def _align_id(card: Card, stem: str) -> Card:
    return card if card.id == stem else card.model_copy(update={"id": stem})


def get_card(card_id: str) -> Card | None:
    p = _path(card_id)
    if not p.exists():
        return None
    try:
        card = parse_card_text(p.read_text(encoding="utf-8"), fallback_id=card_id)
    except (CardError, OSError):
        return None
    card = _align_id(card, card_id)
    _cache[str(p)] = (p.stat().st_mtime, card)
    return card


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def display_path(path: Path) -> str:
    """展示用路径：在仓库内给相对路径，在仓库外（ASC_AGENT_KNOWLEDGE 指到别处）给绝对路径。"""
    try:
        return str(path.relative_to(config.REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def save_card(card: Card, create_only: bool = False) -> dict:
    """写入卡片。create_only=True 时若已存在则报错（防误覆盖）。

    注意 `created` 的继承：文件存在但**解析失败**时 `get_card()` 会返回 None
    （例如用户手工编辑写坏了 frontmatter），这里必须容错，否则「用工具修复坏卡片」
    这个最常见的场景反而会崩在 AttributeError 上。
    """
    path = _path(card.id)
    existed = path.exists()
    if create_only and existed:
        raise CardError(f"卡片 {card.id!r} 已存在；改内容请用 save（不带 create_only）")
    today = date.today().isoformat()
    if not card.created:
        prev = get_card(card.id) if existed else None
        card.created = (prev.created if prev is not None else "") or card.updated or today
    card.updated = today
    _atomic_write(path, render_card_text(card))
    _cache.pop(str(path), None)
    return {"id": card.id, "path": display_path(path), "created": not existed}


def delete_card(card_id: str) -> dict:
    p = _path(card_id)
    if not p.exists():
        raise CardError(f"卡片 {card_id!r} 不存在")
    # 先找出引用它的卡片，删完要让用户知道有哪些悬空关系
    referrers = [
        c.id for c in list_cards() if any(r.target == card_id for r in c.relations)
    ]
    p.unlink()
    _cache.pop(str(p), None)
    return {"id": card_id, "deleted": True, "referrers": referrers}


# ---------------------------------------------------------------------------
# 关系图
# ---------------------------------------------------------------------------


def graph(card: Card) -> dict:
    """返回出边与入边（入边由反向推导，不允许双侧各存一份）。

    对称关系（contradicts/alternative/related）反向名相同；有向关系用 INVERSE
    映射，例如 A `refines` B 时，B 的入边显示为 `refined_by?` 之外的
    `generalizes` 反向——具体见 `INVERSE`。
    """
    outgoing = [
        {"type": r.type.value, "target": r.target, "note": r.note} for r in card.relations
    ]
    incoming: list[dict] = []
    for other in list_cards():
        if other.id == card.id:
            continue
        for r in other.relations:
            if r.target != card.id:
                continue
            rev = INVERSE.get(r.type.value, RelationType.related.value)
            incoming.append({"type": rev, "source": other.id, "note": r.note})
    return {
        "outgoing": outgoing,
        "incoming": incoming,
        "asymmetric": sorted(ASYMMETRIC),
    }


def link(source_id: str, target_id: str, rel_type: str, note: str = "") -> dict:
    """建立关系。只写在 source 上，target 侧靠推导。"""
    if source_id == target_id:
        raise CardError("不能建立自环关系")
    source, target = get_card(source_id), get_card(target_id)
    if source is None:
        raise CardError(f"源卡片 {source_id!r} 不存在")
    if target is None:
        raise CardError(f"目标卡片 {target_id!r} 不存在")
    try:
        rtype = RelationType(rel_type)
    except ValueError as exc:
        raise CardError(
            f"未知关系类型 {rel_type!r}，可选："
            + ", ".join(t.value for t in RelationType)
        ) from exc
    if any(r.target == target_id and r.type == rtype for r in source.relations):
        return {"source": source_id, "target": target_id, "type": rtype.value, "changed": False}
    source.relations.append(Relation(type=rtype, target=target_id, note=note))
    save_card(source)
    return {"source": source_id, "target": target_id, "type": rtype.value, "changed": True}


def unlink(source_id: str, target_id: str, rel_type: str = "") -> dict:
    source = get_card(source_id)
    if source is None:
        raise CardError(f"源卡片 {source_id!r} 不存在")
    before = len(source.relations)
    source.relations = [
        r
        for r in source.relations
        if not (r.target == target_id and (not rel_type or r.type.value == rel_type))
    ]
    removed = before - len(source.relations)
    if removed:
        save_card(source)
    return {"source": source_id, "target": target_id, "removed": removed}


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def _haystack(card: Card) -> str:
    parts = [
        card.title * 3,
        card.summary * 2,
        " ".join(card.tags) * 2,
        " ".join(
            v
            for dim in card.scope.model_dump().values()
            for v in (dim or [])
        ),
        card.body,
        card.id.replace("-", " "),
    ]
    return "\n".join(parts).lower()


def search_cards(
    query: str,
    scope: Scope | None = None,
    card_type: str = "",
    status: str = "",
    limit: int = 8,
    include_deprecated: bool = False,
) -> list[dict]:
    """关键词检索 + 作用域过滤。

    卡片量级小，直接全量打分即可；不引入第二个索引，避免与文件真相不同步。
    """
    terms = [t for t in highlight_terms(query) if t]
    scope = scope or Scope()
    scored: list[tuple[float, Card]] = []

    for card in list_cards():
        if card_type and card.type.value != card_type:
            continue
        if status:
            if card.status.value != status:
                continue
        elif not include_deprecated and card.status.value == "deprecated":
            continue
        if scope and not card.scope.matches(scope):
            continue

        hay = _haystack(card)
        if terms:
            hit = sum(1 for t in terms if t in hay)
            if not hit:
                continue
            score = hit / len(terms) * 10.0
        else:
            score = 1.0
        # 标题命中权重大：查「DataCopy 对齐」时标题里有 DataCopy 的卡更相关
        title = card.title.lower()
        score += sum(3.0 for t in terms if t in title)
        score += sum(1.5 for t in terms if t in " ".join(card.tags).lower())
        # 已验证的卡优先于草稿，过时卡整体靠后
        score += {"verified": 1.5, "draft": 0.0, "deprecated": -3.0}[card.status.value]
        score += {"high": 0.6, "medium": 0.3, "low": 0.0}[card.confidence.value]
        scored.append((score, card))

    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[dict] = []
    for score, card in scored[:limit]:
        item = card.brief()
        item["score"] = round(score, 2)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 一致性检查
# ---------------------------------------------------------------------------

KNOWN_MODELS = {
    "c_api", "tensor_api", "tpipe", "simt", "simd", "scalar",
    "aicpu", "asc_stdlib", "inline_asm", "host",
}


def check() -> dict:
    """体检：悬空关系、自环、取代关系未标 deprecated、作用域笔误等。"""
    cards = list_cards(reload=True)
    ids = {c.id for c in cards}
    issues: list[dict] = []

    # 目录里有但解析不了的文件；顺带查文件名与 frontmatter id 是否一致
    for p in sorted(_root().glob("*.md")):
        try:
            raw = parse_card_text(p.read_text(encoding="utf-8"), fallback_id=p.stem)
        except (CardError, OSError) as exc:
            issues.append(
                {"card": p.stem, "level": "error", "code": "unparsable", "message": str(exc)}
            )
            continue
        if raw.id != p.stem:
            issues.append(
                {"card": p.stem, "level": "error", "code": "id-filename-mismatch",
                 "message": f"frontmatter id 是 {raw.id!r}，但文件名是 {p.stem!r}；"
                            "寻址以文件名为准，请把两处改成一致"}
            )

    for card in cards:
        if card.id != card.id.lower():
            issues.append(
                {"card": card.id, "level": "warn", "code": "id-case",
                 "message": "id 建议全小写，避免跨平台文件名不一致"}
            )
        for r in card.relations:
            if r.target == card.id:
                issues.append(
                    {"card": card.id, "level": "error", "code": "self-loop",
                     "message": f"自环关系 {r.type.value} → {r.target}"}
                )
                continue
            if r.target not in ids:
                issues.append(
                    {"card": card.id, "level": "error", "code": "dangling",
                     "message": f"关系 {r.type.value} 指向不存在的卡片 {r.target!r}"}
                )
                continue
            if r.type == RelationType.supersedes:
                target = get_card(r.target)
                if target and target.status.value != "deprecated":
                    issues.append(
                        {"card": card.id, "level": "warn", "code": "supersede-status",
                         "message": f"取代了 {r.target}，但对方 status 不是 deprecated"}
                    )
        for model in card.scope.programming_model:
            if model.lower() not in KNOWN_MODELS:
                issues.append(
                    {"card": card.id, "level": "warn", "code": "unknown-model",
                     "message": f"未识别的编程模型 {model!r}（已知：{', '.join(sorted(KNOWN_MODELS))}）"}
                )
        if card.status == "verified" and not card.verified_on:
            issues.append(
                {"card": card.id, "level": "warn", "code": "unverified",
                 "message": "status=verified 但没有 verified_on 记录（芯片/CANN 版本/日期）"}
            )
        if not card.summary:
            issues.append(
                {"card": card.id, "level": "info", "code": "no-summary",
                 "message": "缺少 summary，列表和检索命中时不好判断相关性"}
            )

    # 冲突卡片必须让人看见
    for card in cards:
        for r in card.relations:
            if r.type == RelationType.contradicts and card.id < r.target:
                issues.append(
                    {"card": card.id, "level": "warn", "code": "conflict",
                     "message": f"与 {r.target} 存在未裁决的冲突，agent 回答时必须两边都提"}
                )

    return {
        "total": len(cards),
        "ok": not any(i["level"] == "error" for i in issues),
        "issues": issues,
        "counts": {
            lvl: sum(1 for i in issues if i["level"] == lvl)
            for lvl in ("error", "warn", "info")
        },
    }
