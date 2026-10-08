"""Offline EMS detail parsing: one complete official recruitment sample, no source crawl."""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from signalnest.contracts import PageInput
from signalnest.ems_parsing import PARSER_VERSION, parse_ems_notice
from signalnest.parsing import PARSER_VERSION as WHU_PARSER_VERSION
from signalnest.parsing import ParseError, ParseErrorCode, parse_notice

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures/teaching-assistant"
NOTICE_FILE = "ems-notice-250571-20261008T102823953399Z.html"
NEWS_FILE = "whu-news-45084-negative-20261008T102828089357Z.html"
NOTICE_URL = "https://ems.whu.edu.cn/info/1588/250571.htm"
LEGACY_URL = (
    "https://ems.whu.edu.cn/2025/content.jsp?urltype=news.NewsContentUrl"
    "&wbtreeid=1588&wbnewsid=250571"
)


def real_page(url=NOTICE_URL):
    return PageInput(content=(FIXTURES / NOTICE_FILE).read_bytes(), page_url=url)


def html_page(body="<p>课程助教选聘报名。</p>", title="课程助教选聘通知", day="2024-09-09"):
    html = (
        '<div class="article"><form name="_newscontent_fromname">'
        f'<h2 id="titleStr">{title}</h2>'
        f'<h4 class="timeandhit">发布时间 ：{day} 阅读：<script>dynamic()</script></h4>'
        f'<div id="vsb_content_2"><div class="v_news_content">{body}</div></div>'
        "</form></div>"
    )
    return PageInput(content=html.encode("utf-8"), page_url=NOTICE_URL)


def changed(page, edit):
    tree = BeautifulSoup(page.content.decode("utf-8"), "html.parser")
    edit(tree)
    return PageInput(content=str(tree).encode("utf-8"), page_url=page.page_url)


def assert_error(page, code, field=None, index=None):
    with pytest.raises(ParseError) as caught:
        parse_ems_notice(page)
    assert caught.value.code == code
    if field is not None:
        assert caught.value.field == field
    if index is not None:
        assert caught.value.item_index == index
    assert len(str(caught.value)) < 150
    assert "<" not in str(caught.value)


def test_real_recruitment_metadata_body_and_attachments():
    page = real_page()
    metadata = json.loads((FIXTURES / NOTICE_FILE.replace(".html", ".json")).read_text())
    assert hashlib.sha256(page.content).hexdigest() == metadata["sha256"]
    assert len(page.content) == metadata["body_bytes"] == 24457
    notice = parse_ems_notice(page)
    assert notice.source_document_id == "1588:250571"
    assert str(notice.page_url) == metadata["final_url"]
    assert notice.parser_version == PARSER_VERSION == "ems-notices-v1"
    assert notice.content.title == "经济与管理学院2024—2025学年第一学期本科教学课程助教选聘通知"
    assert notice.content.published_date == date(2024, 9, 9)
    assert "原则上从本院全日制研究生中选聘" in notice.content.body_text
    assert "2024年9月20日（周五）下午16点前" in notice.content.body_text
    assert "<p" in notice.content.body_html
    assert "2024年9月9日" in notice.content.body_text
    assert not notice.content.images
    assert not notice.content.links
    assert len(notice.content.attachments) == 2
    first, second = notice.content.attachments
    assert first.name == "附件一：武汉大学经济与管理学院本科教学课程助教岗位申请审核表.doc"
    assert second.name == "附件二：武汉大学经济与管理学院本科教学课程助教考核表.docx"
    assert first.source_attachment_id == "1583652257:8264A536C9AF4EC033B932F57981E805"
    assert second.source_attachment_id == "1583652257:5001118ED1C164A685DD205F19158BA5"
    assert all(item.url.host == "ems.whu.edu.cn" for item in notice.content.attachments)
    assert all(item.access == "not_checked" for item in notice.content.attachments)
    assert "已下载" not in notice.content.body_text
    assert "上一条" not in notice.content.body_text
    assert "_showDynClicks" not in notice.content.body_html
    assert "nattach" not in notice.content.body_html


@pytest.mark.parametrize(
    "url",
    [
        NOTICE_URL,
        NOTICE_URL + "?unrelated=1",
        LEGACY_URL,
        "https://ems.whu.edu.cn/2025/content.jsp?unused=1&wbnewsid=250571&wbtreeid=1588",
        NOTICE_URL + "?wbtreeid=1588&wbnewsid=250571",
    ],
)
def test_supported_detail_routes_share_identity_and_content(url):
    notice = parse_ems_notice(real_page(url))
    assert notice.source_document_id == "1588:250571"
    assert notice.content == parse_ems_notice(real_page()).content


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://news.whu.edu.cn/info/1588/250571.htm", "unsupported_identity"),
        ("https://auth.whu.edu.cn/info/1588/250571.htm", "unsupported_identity"),
        ("https://ems.whu.edu.cn:444/info/1588/250571.htm", "unsupported_identity"),
        ("https://ems.whu.edu.cn/info/1589/250571.htm", "unsupported_identity"),
        ("https://ems.whu.edu.cn/info/1588/0250571.htm", "unsupported_identity"),
        ("https://ems.whu.edu.cn/system/resource/code/auth/caslogin.jsp", "unsupported_identity"),
        (
            "https://ems.whu.edu.cn/2024/content.jsp?wbtreeid=1588&wbnewsid=1",
            "unsupported_identity",
        ),
        (NOTICE_URL + "#body", "unsupported_identity"),
        (NOTICE_URL + "?wbnewsid=1", "ambiguous_identity"),
        (NOTICE_URL + "?wbtreeid=1588&wbtreeid=1588", "ambiguous_identity"),
        (NOTICE_URL + "?urltype=other", "unsupported_identity"),
        ("https://ems.whu.edu.cn/2025/content.jsp?wbtreeid=1588", "invalid_field"),
        ("https://ems.whu.edu.cn/2025/content.jsp?wbnewsid=1", "invalid_field"),
        ("https://ems.whu.edu.cn/2025/content.jsp?wbtreeid=1588&wbnewsid=", "invalid_field"),
        (
            "https://ems.whu.edu.cn/2025/content.jsp?wbtreeid=1589&wbnewsid=1",
            "unsupported_identity",
        ),
        (
            "https://ems.whu.edu.cn/2025/content.jsp?wbtreeid=1588&wbnewsid=1&wbnewsid=2",
            "ambiguous_identity",
        ),
    ],
)
def test_unsupported_or_ambiguous_article_identity_fails(url, code):
    assert_error(real_page(url), code, "identity")


def test_official_personal_news_is_not_an_ems_notice_even_with_spoofed_url():
    html = (FIXTURES / NEWS_FILE).read_bytes()
    assert_error(
        PageInput(content=html, page_url="https://news.whu.edu.cn/info/1002/45084.htm"),
        "unsupported_identity",
        "identity",
    )
    assert_error(PageInput(content=html, page_url=NOTICE_URL), "missing_structure", "notice")


@pytest.mark.parametrize(
    ("selector", "field"),
    [
        ("div.article", "notice"),
        ('form[name="_newscontent_fromname"]', "notice"),
        ("h2#titleStr", "title"),
        ("h4.timeandhit", "published_date"),
        ("div#vsb_content_2", "body"),
        (".v_news_content", "body"),
    ],
)
def test_missing_required_template_node_is_not_success(selector, field):
    assert_error(
        changed(real_page(), lambda tree: tree.select_one(selector).decompose()),
        "missing_structure",
        field,
    )


@pytest.mark.parametrize("selector", ["h2#titleStr", "h4.timeandhit", "#vsb_content_2"])
def test_duplicate_required_node_fails(selector):
    def duplicate(tree):
        node = tree.select_one(selector)
        node.insert_after(BeautifulSoup(str(node), "html.parser"))

    assert_error(changed(real_page(), duplicate), "missing_structure")


@pytest.mark.parametrize("day", ["2024-02-30", "09-09-2024", "2024/09/09", ""])
def test_invalid_visible_date_fails(day):
    assert_error(html_page(day=day), "invalid_field", "published_date")


@pytest.mark.parametrize("body", ["", " \n ", "<script>报名</script>", "<div hidden>报名</div>"])
def test_empty_or_decorative_body_fails(body):
    assert_error(html_page(body=body), "meaningless_body", "body")


def test_image_only_body_and_http_references_follow_shared_rules():
    page = html_page(
        '<p><a href="../apply.htm">报名</a><a href="https://example.org/apply">校外申请</a>'
        '<a href="#notice">定位</a><a href="mailto:test@example.org">联系</a>'
        '<a href="javascript:void(0)">菜单</a></p>'
        '<img src="../../images/poster.jpg" alt="招聘海报">'
        '<img src="/counter.gif" width="1" height="1">'
    )
    content = parse_ems_notice(page).content
    assert [str(link.url) for link in content.links] == [
        "https://ems.whu.edu.cn/info/apply.htm",
        "https://example.org/apply",
    ]
    assert len(content.images) == 1
    assert str(content.images[0].url) == "https://ems.whu.edu.cn/images/poster.jpg"
    assert content.images[0].alt_text == "招聘海报"
    assert 'href="#notice"' not in content.body_html
    assert "mailto:" not in content.body_html
    assert "javascript:" not in content.body_html
    image_only = parse_ems_notice(html_page(body='<img src="/images/poster.jpg">')).content
    assert image_only.body_text == ""
    assert len(image_only.images) == 1


def test_bad_reference_and_invalid_utf8_fail_without_raw_html_in_error():
    assert_error(html_page(body='<img src="http://">'), "invalid_field", "image_url")
    assert_error(PageInput(content=b"\xff", page_url=NOTICE_URL), "invalid_utf8")
    assert_error(PageInput(content=b" \n", page_url=NOTICE_URL), "empty_page")
    assert_error(
        PageInput(content=b"<html><h1>login</h1></html>", page_url=NOTICE_URL),
        "missing_structure",
        "notice",
    )


@pytest.mark.parametrize(
    ("href", "code", "field"),
    [
        ("javascript:download()", "invalid_field", "attachment_url"),
        ("/system/_content/download.jsp?owner=1", "invalid_field", "attachment_identity"),
        (
            "/system/_content/download.jsp?owner=1&wbfileid=ABC&wbfileid=DEF",
            "ambiguous_identity",
            "attachment_identity",
        ),
        (
            "https://other.example.org/system/_content/download.jsp?owner=1&wbfileid=ABC",
            "invalid_field",
            "attachment_url",
        ),
    ],
)
def test_invalid_attachment_cannot_be_silently_lost(href, code, field):
    def edit(tree):
        tree.select_one("#vsb_content_2 > p:not(.pre):not(.next) > a")["href"] = href

    assert_error(changed(real_page(), edit), code, field, 0)


def test_repeated_parse_and_stats_or_outside_changes_keep_digest():
    original = real_page()
    first = parse_ems_notice(original)
    assert parse_ems_notice(original) == first

    def edit_stats(tree):
        tree.select_one("h4.timeandhit").string = "发布时间 ：2024-09-09 阅读：99999"
        tree.select_one("#nattach14978602").string = "99999"
        tree.select_one("p.pre a").string = "另一条导航"
        tree.select_one("title").string = "站外区域变动"

    edited = changed(original, edit_stats)
    assert original.content != edited.content
    assert parse_ems_notice(edited).content.content_sha256() == first.content.content_sha256()

    def edit_body(tree):
        tree.select_one(".v_news_content p").string = "本次新增了一项真实申请要求。"

    modified = changed(original, edit_body)
    assert parse_ems_notice(modified).content.content_sha256() != first.content.content_sha256()


def test_failed_calls_do_not_poison_later_success_or_enable_ems_in_whu_parser():
    broken = html_page(body="<script>正文</script>")
    for _ in range(2):
        assert_error(broken, ParseErrorCode.MEANINGLESS_BODY, "body")
    assert parse_ems_notice(real_page()).source_document_id == "1588:250571"
    with pytest.raises(ParseError) as caught:
        parse_notice(real_page())
    assert caught.value.code == ParseErrorCode.UNSUPPORTED_IDENTITY
    assert WHU_PARSER_VERSION == "whu-student-notices-v3"
