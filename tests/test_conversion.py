"""Converter, PII removal, LLM reformat and revision diffs (no network, no database)."""

import json
from types import SimpleNamespace

import pytest

from app.services.conversion import pii
from app.services.conversion.converter import ConversionError, ConversionResult, convert
from app.services.conversion.llm_format import reformat_with_llm
from app.services.revisions import make_diff
from app.services.uploads import EXTENSION_MIME
from tests import fixtures_docs as docs


def md(data: bytes, ext: str) -> ConversionResult:
    return convert(data, f"file{ext}", EXTENSION_MIME[ext])


# ---- formats ---------------------------------------------------------------------------


def test_xlsx_every_sheet_as_table_without_nan() -> None:
    data = docs.xlsx(
        {
            "มุกคำพ้อง": [["คำ", "มุก"], ["รองเท้า", "กีโต้"], ["มะม่วง", None]],
            "อื่นๆ": [["a"], ["b"]],
        }
    )
    out = md(data, ".xlsx")
    assert out.method == "markitdown"
    assert "## มุกคำพ้อง\n| คำ | มุก |" in out.markdown
    assert "| รองเท้า | กีโต้ |" in out.markdown
    assert "| มะม่วง | |" in out.markdown
    assert "NaN" not in out.markdown
    assert "## อื่นๆ" in out.markdown


def test_pdf_marks_pages() -> None:
    out = md(docs.pdf(["Page one joke", "Page two joke"]), ".pdf")
    assert out.markdown == (
        '<!-- loc: {"page": 1} -->\nPage one joke\n\n<!-- loc: {"page": 2} -->\nPage two joke\n'
    )


def test_pdf_without_text_is_unreadable() -> None:
    with pytest.raises(ConversionError) as info:
        md(docs.pdf([""]), ".pdf")
    assert info.value.code == "UNREADABLE_IMAGE"


def test_pptx_slides_become_loc_comments() -> None:
    out = md(docs.pptx(["มุกวันจันทร์", "มุกวันศุกร์"]), ".pptx")
    assert '<!-- loc: {"slide": 1} -->\n# มุกวันจันทร์' in out.markdown
    assert '<!-- loc: {"slide": 2} -->' in out.markdown


def test_docx_paragraphs() -> None:
    out = md(docs.docx(["ย่อหน้าแรก มุกฮา", "ย่อหน้าสอง"]), ".docx")
    assert out.markdown == "ย่อหน้าแรก มุกฮา\n\nย่อหน้าสอง\n"


def test_csv_utf8_and_cp874() -> None:
    text = 'คำ,มุก\nรองเท้า,กีโต้\nสีเหลือง,"เยลโล่, ฮา"\n'
    for encoding in ("utf-8", "cp874"):
        out = md(text.encode(encoding), ".csv")
        assert "| สีเหลือง | เยลโล่, ฮา |" in out.markdown


def test_json_list_of_objects_becomes_table() -> None:
    data = [{"q": "มีดอะไรใหญ่สุด", "a": "อีโต้"}, {"q": "a|b", "extra": None}]
    out = md(json.dumps(data, ensure_ascii=False).encode(), ".json")
    assert out.method == "json"
    assert out.markdown.splitlines() == [
        "| q | a | extra |",
        "| --- | --- | --- |",
        "| มีดอะไรใหญ่สุด | อีโต้ |  |",
        "| a\\|b |  |  |",
    ]


def test_nested_json_becomes_code_block() -> None:
    out = md(b'{"jokes": [{"a": 1}]}', ".json")
    assert out.markdown.startswith("```json\n{\n")


def test_invalid_json_and_images_fail_permanently() -> None:
    with pytest.raises(ConversionError):
        md(b"{oops", ".json")
    with pytest.raises(ConversionError) as info:
        md(b"\x89PNG", ".png")
    assert info.value.code == "UNREADABLE_IMAGE"


def test_embedded_images_are_dropped() -> None:
    out = md("มุก ![รูป](data:image/png;base64,AAA) ฮา".encode(), ".md")
    assert out.markdown == "มุก  ฮา\n"


def test_markup_only_is_empty_content() -> None:
    with pytest.raises(ConversionError) as info:
        md(b'<!-- loc: {"page": 1} -->\n| |\n| --- |\n', ".md")
    assert info.value.code == "EMPTY_CONTENT"


# ---- personal data ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "081-234-5678",
        "0812345678",
        "081 234 5678",
        "+66 81 234 5678",
        "02-123-4567",
        "me.name+tag@example.co.th",
        "1-1037-02071-81-1",  # valid checksum
        "1103702071811",
    ],
)
def test_pii_is_redacted(value: str) -> None:
    text, counts = pii.redact(f"ติดต่อ {value} นะ")
    assert text == f"ติดต่อ {pii.REDACTED} นะ"
    assert sum(counts.values()) == 1


@pytest.mark.parametrize(
    "value",
    [
        "1103702071812",  # bad checksum: not an ID
        "ปี 2567 ราคา 199 บาท",
        "12345",
        '<!-- loc: {"page": 12} -->',
    ],
)
def test_non_pii_is_kept(value: str) -> None:
    assert pii.redact(value) == (value, {})


def test_conversion_removes_pii() -> None:
    out = md("มุก โทร 0891234567 อีเมล a@b.com".encode(), ".txt")
    assert out.markdown == f"มุก โทร {pii.REDACTED} อีเมล {pii.REDACTED}\n"
    assert out.pii_removed == {"phone": 1, "email": 1}


# ---- LLM reformat ----------------------------------------------------------------------


class FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.sent: list = []

    async def chat(self, role, messages, **kwargs):
        self.sent.append((role, messages, kwargs))
        return SimpleNamespace(text=self.reply, model="lite")


async def test_llm_reformat_strips_fence_and_redacts() -> None:
    base = ConversionResult("รองเท้า กีโต้ มะม่วง แมงโก้ โทร 0812345678\n", "text", {"phone": 1})
    llm = FakeLLM("```markdown\n- รองเท้า → กีโต้\n- มะม่วง → แมงโก้ a@b.co\n```")
    out = await reformat_with_llm(base, llm)  # type: ignore[arg-type]
    assert out.markdown == f"- รองเท้า → กีโต้\n- มะม่วง → แมงโก้ {pii.REDACTED}\n"
    assert out.method == "llm:lite"
    assert out.pii_removed == {"phone": 1, "email": 1}
    role, messages, kwargs = llm.sent[0]
    assert role == "internal" and kwargs["temperature"] == 0
    assert "0812345678" not in messages[1]["content"]  # PII never sent to the provider


async def test_llm_reformat_rejects_suspiciously_short_output() -> None:
    base = ConversionResult("มุก " * 200, "text")
    with pytest.raises(ConversionError):
        await reformat_with_llm(base, FakeLLM("- สรุป"))  # type: ignore[arg-type]


async def test_llm_reformat_rejects_huge_input() -> None:
    with pytest.raises(ConversionError):
        await reformat_with_llm(ConversionResult("ก" * 40_000, "text"), FakeLLM("x"))  # type: ignore[arg-type]


def test_make_diff() -> None:
    assert make_diff(None, "a\n") is None
    diff = make_diff("a\nb\n", "a\nc\n")
    assert "-b" in diff and "+c" in diff  # type: ignore[operator]
