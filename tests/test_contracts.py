import json
from datetime import date

import pytest
from pydantic import ValidationError

from signalnest.contracts import (
    AttachmentReference,
    ImageReference,
    ListEntry,
    ListPage,
    NoticeContent,
    PageInput,
    ParsedNotice,
    RawResponseReference,
)

URL = "https://uc.whu.edu.cn/info/1517/128231.htm"


def content(**overrides):
    return NoticeContent(
        **(
            {
                "title": "选课通知",
                "published_date": date(2026, 9, 4),
                "body_html": "<p>正文</p>",
                "body_text": "正文",
                "images": [ImageReference(url="https://uc.whu.edu.cn/images/calendar.png")],
                "attachments": [
                    AttachmentReference(
                        name="安排.pdf",
                        url="https://uc.whu.edu.cn/system/_content/download.jsp?wbfileid=1",
                        source_attachment_id="1407739091:1",
                    )
                ],
            }
            | overrides
        )
    )


def test_notice_roundtrip_and_digest():
    notice = ParsedNotice(
        source_document_id="1517:128231",
        page_url=URL,
        parser_version="whu-v1",
        content=content(),
    )
    restored = ParsedNotice.model_validate_json(notice.model_dump_json())
    assert restored == notice
    assert restored.content.content_sha256() == notice.content.content_sha256()
    assert len(restored.content.content_sha256()) == 64
    assert json.loads(restored.content.canonical_json())["published_date"] == "2026-09-04"
    assert "<p>正文</p>" not in repr(notice)
    assert "<p>正文</p>" not in repr(notice.content)


def test_digest_tracks_published_content_not_parser_or_download_state():
    original = content()
    assert content(body_text="修改正文").content_sha256() != original.content_sha256()
    changed_access = original.attachments[0].model_copy(update={"access": "manual_required"})
    assert content(attachments=[changed_access]).content_sha256() == original.content_sha256()
    first = ParsedNotice(
        source_document_id="1517:128231",
        page_url=URL,
        parser_version="whu-v1",
        content=original,
    )
    second = first.model_copy(update={"parser_version": "whu-v2"})
    assert first.content.content_sha256() == second.content.content_sha256()


def test_list_page_resolved_links_and_identity():
    entry = ListEntry(
        source_document_id="1517:128231",
        detail_url=URL,
        title=" 通知 ",
        published_date="2026-09-04",
    )
    page = ListPage(entries=[entry], next_page_url="https://uc.whu.edu.cn/tzgg/xstz/23.htm")
    assert page.entries == (entry,)
    assert entry.title == "通知"
    assert ListPage(entries=[entry]).next_page_url is None
    with pytest.raises(ValidationError):
        entry.title = "changed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "  "},
        {"body_html": ""},
        {"published_date": "2026-99-99"},
        {"unknown": 1},
    ],
)
def test_notice_contract_rejects_invalid_data(overrides):
    with pytest.raises(ValidationError):
        content(**overrides)


def test_empty_list_and_relative_or_credential_urls_rejected():
    with pytest.raises(ValidationError):
        ListPage(entries=[])
    for url in ["../info/1517/1.htm", "file:///tmp/a", "https://user:secret@example.org/a"]:
        with pytest.raises(ValidationError):
            PageInput(content=b"<html></html>", page_url=url)
    with pytest.raises(ValidationError):
        PageInput(content=b"", page_url=URL)
    assert "private page text" not in repr(PageInput(content=b"private page text", page_url=URL))


def test_raw_reference_requires_safe_digest_path_and_no_304_body():
    values = dict(
        source_id="whu-undergrad-student",
        requested_url=URL,
        final_url=URL,
        fetched_at=100,
        status_code=200,
        body_path="raw/" + "a" * 64 + ".bin",
        body_sha256="a" * 64,
    )
    assert RawResponseReference(**values).body_sha256 == "a" * 64
    assert (
        RawResponseReference(
            **(
                values
                | {
                    "status_code": 304,
                    "body_path": None,
                    "body_sha256": None,
                }
            )
        ).body_path
        is None
    )
    for overrides in [
        {"status_code": 304},
        {"body_path": "../secret"},
        {"body_sha256": None},
        {"body_sha256": "bad"},
        {"fetched_at": -1},
        {"status_code": 600},
    ]:
        with pytest.raises(ValidationError):
            RawResponseReference(**(values | overrides))
