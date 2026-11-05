"""知识卡片系统测试：文件存储、作用域过滤、关系推导、体检。

每个用例都在临时目录里跑（monkeypatch `config.CARDS_DIR`），不碰仓库里的真卡片。
"""

from __future__ import annotations

import pytest

from asc_agent import config
from asc_agent.knowledge import store
from asc_agent.knowledge.models import Card, Scope
from asc_agent.mcp_server import tools_kb as kb


@pytest.fixture(autouse=True)
def temp_cards(tmp_path, monkeypatch):
    """把卡片目录指到临时路径，并清掉 mtime 缓存。"""
    d = tmp_path / "cards"
    d.mkdir()
    monkeypatch.setattr(config, "CARDS_DIR", d)
    store._cache.clear()
    yield d
    store._cache.clear()


def make(card_id: str = "c1", **kw):
    base = dict(id=card_id, title=f"标题 {card_id}", type="technique")
    base.update(kw)
    return kb.card_save(**base)


# ---------------------------------------------------------------- 基本读写


def test_create_writes_file_with_frontmatter(temp_cards):
    out = make("datacopy-tail", summary="尾块要 Pad", api="DataCopyPad")
    assert out["created"] is True
    path = temp_cards / "datacopy-tail.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "id: datacopy-tail" in text
    assert "api:" in text


def test_roundtrip_preserves_fields(temp_cards):
    make(
        "c-roundtrip",
        title="标题",
        summary="一句话",
        body="## 背景\n\n正文",
        architecture="2201,3510",
        api="Exp",
        tags="精度,对齐",
        evidence_path="docs/zh/api/x.md",
        verified_chip="ascend910b4",
        confidence="high",
        status="verified",
    )
    card = store.get_card("c-roundtrip")
    assert card is not None
    assert card.scope.architecture == ["2201", "3510"]
    assert card.tags == ["精度", "对齐"]
    assert card.verified_on and card.verified_on.chip == "ascend910b4"
    assert "正文" in card.body


def test_new_card_requires_title_and_type(temp_cards):
    out = kb.card_save(id="no-title")
    assert "error" in out
    assert "title" in out["error"]


def test_illegal_id_rejected(temp_cards):
    out = make("Bad_ID_With_Caps")
    assert "error" in out


def test_create_only_protects_existing(temp_cards):
    make("dup")
    out = make("dup", create_only=True)
    assert "error" in out
    assert "已存在" in out["error"]


# ---------------------------------------------------------------- 更新语义


def test_update_only_touches_supplied_fields(temp_cards):
    make("patch", title="原标题", summary="原摘要", body="原正文", api="Exp")
    kb.card_save(id="patch", summary="新摘要")
    card = store.get_card("patch")
    assert card.title == "原标题"      # 未传 → 保留
    assert card.summary == "新摘要"
    assert card.body == "原正文"
    assert card.scope.api == ["Exp"]


def test_empty_string_clears_field(temp_cards):
    make("clear", summary="非空")
    kb.card_save(id="clear", summary="")
    assert store.get_card("clear").summary == ""


def test_updated_date_is_bumped(temp_cards):
    make("date1")
    first = store.get_card("date1").updated
    kb.card_save(id="date1", summary="改一下")
    assert store.get_card("date1").updated == first  # 同一天内不变，但字段存在
    assert first


# ---------------------------------------------------------------- 检索与作用域


def test_search_finds_by_body_keyword(temp_cards):
    make("body-hit", body="实测发现 UB bank 冲突会掉性能")
    hits = kb.card_search("bank 冲突")["hits"]
    assert [h["id"] for h in hits] == ["body-hit"]


def test_scope_filter_by_architecture(temp_cards):
    make("arch-950", summary="subnormal", architecture="3510")
    make("arch-generic", summary="subnormal")
    only_950 = [h["id"] for h in kb.card_search("subnormal", architecture="3510")["hits"]]
    only_910 = [h["id"] for h in kb.card_search("subnormal", architecture="2201")["hits"]]
    assert "arch-950" in only_950
    assert "arch-950" not in only_910      # 卡片比查询更具体 → 不命中
    assert "arch-generic" in only_910      # 通用卡两者都覆盖


def test_universal_scope_matches_everything():
    universal = Scope()
    assert universal.is_universal()
    assert universal.matches(Scope(architecture=["3510"], api=["Exp"]))


def test_scoped_card_is_not_universal():
    assert not Scope(architecture=["3510"]).is_universal()


def test_deprecated_hidden_by_default(temp_cards):
    make("old", summary="旧结论", status="deprecated")
    assert kb.card_search("旧结论")["hits"] == []
    with_old = kb.card_search("旧结论", include_deprecated=True)["hits"]
    assert [h["id"] for h in with_old] == ["old"]


def test_verified_ranks_above_draft(temp_cards):
    make("draft-card", title="对齐 草稿", status="draft")
    make("verified-card", title="对齐 已验证", status="verified", verified_chip="910b")
    hits = kb.card_search("对齐")["hits"]
    assert hits[0]["id"] == "verified-card"


# ---------------------------------------------------------------- 关系


def test_link_and_inverse_derivation(temp_cards):
    make("specific")
    make("general")
    out = kb.card_link("specific", "general", "refines", "特化版本")
    assert out["changed"] is True

    g = kb.card_get("specific")["graph"]
    assert g["outgoing"] == [{"type": "refines", "target": "general", "note": "特化版本"}]

    # 反向在读取时推导：general 的入边应是 generalizes
    incoming = kb.card_get("general")["graph"]["incoming"]
    assert incoming[0]["type"] == "generalizes"
    assert incoming[0]["source"] == "specific"


def test_symmetric_relation_inverse_is_itself(temp_cards):
    make("a")
    make("b")
    kb.card_link("a", "b", "contradicts", "结论冲突")
    incoming = kb.card_get("b")["graph"]["incoming"]
    assert incoming[0]["type"] == "contradicts"


def test_link_is_idempotent(temp_cards):
    make("x")
    make("y")
    kb.card_link("x", "y", "related")
    again = kb.card_link("x", "y", "related")
    assert again["changed"] is False
    assert len(store.get_card("x").relations) == 1


def test_self_loop_rejected(temp_cards):
    make("solo")
    out = kb.card_link("solo", "solo", "related")
    assert "error" in out and "自环" in out["error"]


def test_unknown_target_rejected(temp_cards):
    make("only-me")
    out = kb.card_link("only-me", "ghost", "related")
    assert "error" in out and "ghost" in out["error"]


def test_unknown_relation_type_lists_options(temp_cards):
    make("p")
    make("q")
    out = kb.card_link("p", "q", "made-up")
    assert "error" in out
    assert "refines" in out["error"]


def test_unlink_removes_relation(temp_cards):
    make("u1")
    make("u2")
    kb.card_link("u1", "u2", "related")
    out = kb.card_link("u1", "u2", "related", remove=True)
    assert out["removed"] == 1
    assert store.get_card("u1").relations == []


# ---------------------------------------------------------------- 体检


def test_check_flags_dangling_relation(temp_cards):
    make("alive")
    card = store.get_card("alive")
    card.relations.append(
        store.Relation(type=store.RelationType.related, target="ghost")
    )
    store.save_card(card)
    issues = kb.card_check()["issues"]
    assert any(i["code"] == "dangling" for i in issues)


def test_check_flags_supersede_without_deprecated(temp_cards):
    make("newer")
    make("older", status="verified", verified_chip="910b")
    kb.card_link("newer", "older", "supersedes")
    issues = kb.card_check()["issues"]
    assert any(i["code"] == "supersede-status" for i in issues)


def test_check_passes_on_clean_kb(temp_cards):
    make("ok-card", status="verified", verified_chip="ascend910b4", summary="干净")
    result = kb.card_check()
    assert result["ok"] is True
    assert result["counts"]["error"] == 0


def test_check_flags_unknown_programming_model(temp_cards):
    make("model-typo", programming_model="c-api")   # 正确写法是 c_api
    issues = kb.card_check()["issues"]
    assert any(i["code"] == "unknown-model" for i in issues)


def test_check_flags_verified_without_record(temp_cards):
    make("claims-verified", status="verified")
    issues = kb.card_check()["issues"]
    assert any(i["code"] == "unverified" for i in issues)


def test_check_reports_conflict_pair_once(temp_cards):
    make("c-a")
    make("c-b")
    kb.card_link("c-a", "c-b", "contradicts")
    conflicts = [i for i in kb.card_check()["issues"] if i["code"] == "conflict"]
    assert len(conflicts) == 1


# ---------------------------------------------------------------- 删除


def test_delete_requires_confirm(temp_cards):
    make("to-delete")
    out = kb.card_delete("to-delete")
    assert out["deleted"] is False
    assert (temp_cards / "to-delete.md").exists()


def test_delete_reports_referrers(temp_cards):
    make("target-card")
    make("source-card")
    kb.card_link("source-card", "target-card", "related")
    out = kb.card_delete("target-card", confirm=True)
    assert out["deleted"] is True
    assert "source-card" in out["referrers"]
    assert out["hint"]


def test_delete_missing_card_errors(temp_cards):
    out = kb.card_delete("nope", confirm=True)
    assert "error" in out


# ---------------------------------------------------------------- 缺省与错误


def test_get_unknown_card_returns_hint(temp_cards):
    out = kb.card_get("does-not-exist")
    assert out["found"] is False
    assert out["hint"]


def test_broken_frontmatter_is_reported_not_crashing(temp_cards):
    (temp_cards / "broken.md").write_text("没有 frontmatter 的正文\n", encoding="utf-8")
    issues = kb.card_check()["issues"]
    assert any(i["code"] == "unparsable" for i in issues)
    # 坏文件不影响其它卡片
    make("healthy")
    assert [c.id for c in store.list_cards()] == ["healthy"]


def test_search_without_any_criteria_errors(temp_cards):
    out = kb.card_search("")
    assert "error" in out


def test_manual_edit_is_picked_up(temp_cards):
    """手工改文件（不经过工具）后，读出来应是新内容——文件是唯一真相。"""
    make("hand-edit", summary="工具写的")
    path = temp_cards / "hand-edit.md"
    text = path.read_text(encoding="utf-8").replace("工具写的", "手改的")
    path.write_text(text, encoding="utf-8")
    store._cache.clear()
    assert store.get_card("hand-edit").summary == "手改的"
