"""审核删除意图：全局序号、「的第」忽略、中文数字。"""

from app.domain.models import ActionItem, TaskDraft
from app.domain.review_intent import looks_like_drop_feedback, parse_cn_int, parse_drop_intent


def _draft(*titles: str) -> TaskDraft:
    items = [ActionItem(id=f"id-{i}", title=title, primary_owner="张伟") for i, title in enumerate(titles, 1)]
    return TaskDraft(items=items)


def test_looks_like_drop_feedback():
    assert looks_like_drop_feedback("第 2 条不是任务，删掉")
    assert looks_like_drop_feedback("删除这条")
    assert not looks_like_drop_feedback("把截止日期改到周五")


def test_parse_cn_int():
    assert parse_cn_int("2") == 2
    assert parse_cn_int("二") == 2
    assert parse_cn_int("两") == 2
    assert parse_cn_int("十") == 10
    assert parse_cn_int("十一") == 11
    assert parse_cn_int("二十") == 20
    assert parse_cn_int("二十一") == 21


def test_drop_arabic_and_multi():
    draft = _draft("甲", "乙", "丙")
    one = parse_drop_intent(feedback="第 2 条不是任务，删掉", draft=draft)
    assert one.matched and one.item_ids == ["id-2"]
    multi = parse_drop_intent(feedback="第2、3条删掉", draft=draft)
    assert multi.item_ids == ["id-2", "id-3"]


def test_drop_chinese_ordinal():
    draft = _draft("甲", "乙", "丙")
    result = parse_drop_intent(feedback="第二条不是任务", draft=draft)
    assert result.item_ids == ["id-2"]


def test_drop_last_and_selected():
    draft = _draft("甲", "乙", "丙")
    last = parse_drop_intent(feedback="最后一条删掉", draft=draft)
    assert last.item_ids == ["id-3"]
    selected = parse_drop_intent(
        feedback="上面那条不是任务",
        draft=draft,
        target_item_id="id-2",
    )
    assert selected.item_ids == ["id-2"]


def test_drop_out_of_range():
    draft = _draft("甲", "乙")
    result = parse_drop_intent(feedback="第 9 条删掉", draft=draft)
    assert result.matched
    assert result.error == "当前只有 2 条"
    assert result.item_ids == []


def test_owner_qualified_ordinal_not_parsed():
    draft = _draft("甲", "乙", "丙")
    result = parse_drop_intent(feedback="张三的第2条不是任务，删掉", draft=draft)
    assert result.matched
    assert result.item_ids == []
    assert result.error and "全局序号" in result.error


def test_explicit_drop_ids():
    draft = _draft("甲", "乙")
    result = parse_drop_intent(
        feedback="",
        draft=draft,
        revision_scope="drop",
        drop_item_ids=["id-1"],
    )
    assert result.item_ids == ["id-1"]


def test_trigger_without_index_uses_selected():
    draft = _draft("甲", "乙")
    result = parse_drop_intent(
        feedback="不是任务，删掉",
        draft=draft,
        target_item_id="id-2",
    )
    assert result.item_ids == ["id-2"]
