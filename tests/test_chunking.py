"""Chunking, Thai tokenisation and tag parsing (no network, no database)."""

import pytest

from app.services.chunking import chunk_markdown
from app.services.indexing import parse_tags
from app.services.thai import normalize, search_text, search_tokens


def contents(md: str, **kw) -> list[str]:
    return [c.content for c in chunk_markdown(md, **kw)]


def test_one_joke_per_line_with_title_and_dedup() -> None:
    md = "แคปชั่นวันเกิด\n\nสุขสันต์วันแก่!\nขอให้หน้าไม่ตรงกับอายุก็พอ!\nสุขสันต์วันแก่!\n"
    chunks = chunk_markdown(md)
    assert [c.content for c in chunks] == ["สุขสันต์วันแก่!", "ขอให้หน้าไม่ตรงกับอายุก็พอ!"]
    assert all(c.location == {"section": "แคปชั่นวันเกิด"} for c in chunks)


def test_table_rows_keep_column_names_and_location() -> None:
    md = "## มุกคำพ้อง\n| คำ | มุก |\n| --- | --- |\n| รองเท้า | กีโต้ |\n| มะม่วง | |\n| a\\|b | ฮา |\n"
    chunks = chunk_markdown(md)
    assert [c.content for c in chunks] == ["คำ: รองเท้า | มุก: กีโต้", "คำ: มะม่วง", "คำ: a|b | มุก: ฮา"]
    assert chunks[0].location == {"section": "มุกคำพ้อง", "row": 1}
    assert chunks[2].location["row"] == 3


def test_single_column_table_uses_value_only() -> None:
    assert contents("| มุก |\n| --- |\n| ไม่ขี้เกียจ แค่ประหยัดพลังงาน |\n") == ["ไม่ขี้เกียจ แค่ประหยัดพลังงาน"]


def test_list_items_with_continuation_lines() -> None:
    md = "- มีดอะไรใหญ่สุด?\n  ตอบ อีโต้\n- สีเหลือง เยลโล่\n1. มุกที่สาม ฮาๆ\n"
    assert contents(md) == ["มีดอะไรใหญ่สุด? ตอบ อีโต้", "สีเหลือง เยลโล่", "มุกที่สาม ฮาๆ"]


def test_loc_comments_and_headings_become_location() -> None:
    md = (
        '<!-- loc: {"page": 1} -->\nย่อหน้าหน้าแรก ยาวพอสมควร\n\n'
        '<!-- loc: {"page": 2} -->\n# บทที่ 2\nย่อหน้าหน้าสอง ยาวพอสมควร\n'
    )
    chunks = chunk_markdown(md)
    assert chunks[0].location == {"page": 1}
    assert chunks[1].location == {"page": 2, "section": "บทที่ 2"}


def test_long_text_is_windowed_with_overlap() -> None:
    long = " ".join(f"คำที่{i}" for i in range(400))
    pieces = contents("ย่อหน้ายาว\n" + long, max_chars=500, overlap_chars=50)
    assert len(pieces) > 3
    assert all(len(p) <= 500 for p in pieces)
    first_tail = pieces[0].split()[-1]
    assert first_tail in pieces[1]  # overlap carries the boundary word over


def test_thai_without_spaces_still_splits() -> None:
    text = "".join(chr(0x0E01 + i * 7 % 47) for i in range(3000))
    pieces = contents("หัว\n\n" + text, max_chars=1000, overlap_chars=100)
    assert all(len(p) <= 1000 for p in pieces)
    assert len(pieces) >= 3
    assert pieces[-1][-10:] == text[-10:]  # nothing lost at the end


def test_code_fence_kept_as_one_block() -> None:
    md = 'ข้อมูล\n\n```json\n{\n  "q": "มุก",\n  "a": "ฮา"\n}\n```\n'
    assert contents(md) == ['```json\n{\n  "q": "มุก",\n  "a": "ฮา"\n}\n```']


def test_noise_is_skipped() -> None:
    assert contents("หัวข้อ\n\n---\n!\nก\n") == []


# ---- Thai ------------------------------------------------------------------------------


def test_search_tokens_split_thai_and_drop_punctuation() -> None:
    assert search_tokens("รองเท้าภาษาอะไรเอ่ย? ตอบ: กีโต้") == [
        "รองเท้า", "ภาษา", "อะไร", "เอ่ย", "ตอบ", "กี", "โต้"
    ]  # fmt: skip
    assert search_text("มุก Mango ๆ CEO") == "มุก mango ceo"


def test_normalize_removes_zero_width_and_extra_spaces() -> None:
    assert normalize("มุก​   ฮา\n") == "มุก ฮา"


# ---- tags ------------------------------------------------------------------------------


def test_parse_tags_from_fenced_reply() -> None:
    reply = (
        '```json\n{"summary": "มุกออฟฟิศ", "category": "มุกออฟฟิศ", "tags": ["งาน", "งาน", " "]}\n```'
    )
    assert parse_tags(reply) == {"summary": "มุกออฟฟิศ", "category": "มุกออฟฟิศ", "tags": ["งาน"]}


@pytest.mark.parametrize("reply", ["ไม่มี JSON", "{oops}"])
def test_parse_tags_rejects_garbage(reply: str) -> None:
    with pytest.raises(ValueError):
        parse_tags(reply)
