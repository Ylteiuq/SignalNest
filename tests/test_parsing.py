"""Offline WHU parsing tests: immutable real pages plus small in-memory variations."""

import hashlib
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError

from signalnest.contracts import PageInput
from signalnest.parsing import PARSER_VERSION, ParseError, ParseErrorCode, parse_list, parse_notice

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures"
LIST_URL = "https://uc.whu.edu.cn/tzgg/xstz.htm"
NOTICE_URL = "https://uc.whu.edu.cn/info/1517/128231.htm"
LEGACY_URL = (
    "https://uc.whu.edu.cn/2022/show.jsp?urltype=news.NewsContentUrl&wbtreeid=1517&wbnewsid=127581"
)


def fixture_page(name, url):
    return PageInput(content=(FIXTURES / name).read_bytes(), page_url=url)


def html_page(html, url=NOTICE_URL):
    return PageInput(content=html.encode("utf-8"), page_url=url)


def notice_html(
    body="<p>报名时间：2026-09-30，人数 25。</p>", title="学生通知", day="时间：2026-09-30"
):
    return (
        f'<div class="news_show"><div class="title_nei"><b>{title}</b><i>{day}</i></div>'
        f'<div id="vsb_content"><div class="v_news_content">{body}</div></div></div>'
    )


LIST_PAGINATION = (
    '<div class="page"><span class="p_pages"><span class="p_no_d">1</span>'
    '<span class="p_no"><a href="xstz/800.htm">2</a></span>'
    '<span class="p_no"><a href="xstz/27.htm">3</a></span>'
    '<span class="p_next"><a href="xstz/800.htm">下页</a></span>'
    '<span class="p_last"><a href="xstz/27.htm">尾页</a></span></span></div>'
)


def list_html(href="../info/1517/128231.htm", title="学生通知", day="2026-09-30", extra=None):
    if extra is None:
        extra = LIST_PAGINATION
    return (
        '<div class="nei_right"><div class="list_txt"><ul class="am-list">'
        f'<li><a href="{href}"><span>{title}</span><i>{day}</i></a></li>'
        f"</ul></div>{extra}</div>"
    )


def altered(page, change):
    tree = BeautifulSoup(page.content.decode("utf-8"), "html.parser")
    change(tree)
    return html_page(str(tree), str(page.page_url))


def assert_error(parser, page, code, field=None, index=None):
    with pytest.raises(ParseError) as caught:
        parser(page)
    error = caught.value
    assert error.code == code
    if field is not None:
        assert error.field == field
    if index is not None:
        assert error.item_index == index
    assert len(str(error)) < 150
    assert "<html" not in str(error)
    return error


@pytest.mark.parametrize(
    ("name", "url", "first_id", "first_title", "first_day", "next_url"),
    [
        (
            "student-notices-page1.html",
            LIST_URL,
            "1517:128491",
            "武汉大学2026年下半年国家普通话水平测试报名通知",
            date(2026, 9, 23),
            "https://uc.whu.edu.cn/tzgg/xstz/23.htm",
        ),
        (
            "student-notices-page2.html",
            "https://uc.whu.edu.cn/tzgg/xstz/23.htm",
            "1517:127581",
            "关于2026-2027学年第一学期选课的通知",
            date(2026, 7, 8),
            "https://uc.whu.edu.cn/tzgg/xstz/22.htm",
        ),
    ],
)
def test_real_lists(name, url, first_id, first_title, first_day, next_url):
    result = parse_list(fixture_page(name, url))
    assert len(result.entries) == 25
    assert result.entries[0].source_document_id == first_id
    assert result.entries[0].title == first_title
    assert result.entries[0].published_date == first_day
    assert str(result.next_page_url) == next_url
    assert all(entry.detail_url.host == "uc.whu.edu.cn" for entry in result.entries)
    assert all(not entry.title.startswith("◆") for entry in result.entries)
    assert len({entry.source_document_id for entry in result.entries}) == 25


def test_old_date_in_front_is_not_skipped_or_used_as_a_stop():
    entries = parse_list(fixture_page("student-notices-page1.html", LIST_URL)).entries
    assert entries[5].source_document_id == "1517:128281"
    assert entries[5].published_date == date(2026, 9, 4)
    assert entries[6].published_date == date(2026, 9, 9)
    assert entries[-1].source_document_id == "1517:127611"


@pytest.mark.parametrize(
    "url",
    [
        "https://uc.whu.edu.cn/info/1517/128231.htm",
        "https://uc.whu.edu.cn/info/1517/128231.htm?unrelated=1",
        "https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517&wbnewsid=128231",
        "https://uc.whu.edu.cn/2022/show.jsp?unused=1&wbnewsid=128231&urltype=news.NewsContentUrl&wbtreeid=1517",
    ],
)
def test_identity_is_shared_by_list_and_notice(url):
    entry = parse_list(html_page(list_html(url), LIST_URL)).entries[0]
    notice = parse_notice(html_page(notice_html(), url))
    assert entry.source_document_id == notice.source_document_id == "1517:128231"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://other.example.org/info/1517/128231.htm", "unsupported_identity"),
        ("https://uc.whu.edu.cn/info/1518/128231.htm", "unsupported_identity"),
        ("https://uc.whu.edu.cn/info/1517/not-an-id.htm", "unsupported_identity"),
        ("https://uc.whu.edu.cn/other.htm", "unsupported_identity"),
        ("https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517", "invalid_field"),
        ("https://uc.whu.edu.cn/2022/show.jsp?wbnewsid=1", "invalid_field"),
        ("https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517&wbnewsid=", "invalid_field"),
        (
            "https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517&wbnewsid=1&wbnewsid=2",
            "ambiguous_identity",
        ),
        (
            "https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517&wbtreeid=1517&wbnewsid=1",
            "ambiguous_identity",
        ),
        ("https://uc.whu.edu.cn/info/1517/128231.htm?wbnewsid=2", "ambiguous_identity"),
        ("https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1518&wbnewsid=1", "unsupported_identity"),
    ],
)
def test_bad_identity_fails_without_silent_row_loss(url, code):
    assert_error(parse_notice, html_page(notice_html(), url), code, "identity")
    valid_unadapted = {
        "https://other.example.org/info/1517/128231.htm": "external",
        "https://uc.whu.edu.cn/info/1518/128231.htm": "unsupported_column",
        "https://uc.whu.edu.cn/other.htm": "unsupported_route",
        "https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1518&wbnewsid=1": "unsupported_column",
    }
    if url in valid_unadapted:
        page = parse_list(html_page(list_html(url), LIST_URL))
        assert not page.entries and page.row_count == 1
        assert len(page.references) == 1
        assert str(page.references[0].resolved_url) == url
        assert page.references[0].reference_kind == valid_unadapted[url]
    else:
        list_code = "invalid_field" if "not-an-id.htm" in url else code
        assert_error(parse_list, html_page(list_html(url), LIST_URL), list_code, "identity", 0)


def test_list_scope_relative_urls_variable_count_and_terminal_page():
    outside = '<nav><a href="/info/1517/9.htm">导航</a></nav>'
    terminal = (
        '<div class="page"><span class="p_pages">'
        '<span class="p_no"><a href="../xstz.htm">1</a></span>'
        '<span class="p_no"><a href="700.htm">2</a></span>'
        '<span class="p_no_d">3</span><span class="p_next_d">下页</span>'
        '<span class="p_last_d">尾页</span></span></div>'
    )
    result = parse_list(
        html_page(
            outside
            + list_html("../../info/1517/1.htm", extra=terminal)
            + '<footer><a href="/info/1517/8.htm">页脚</a></footer>',
            "https://uc.whu.edu.cn/tzgg/xstz/1.htm",
        )
    )
    assert len(result.entries) == 1
    assert result.entries[0].source_document_id == "1517:1"
    assert str(result.entries[0].detail_url) == "https://uc.whu.edu.cn/info/1517/1.htm"
    assert result.next_page_url is None
    assert result.pagination.is_last_page
    assert result.pagination.terminal_evidence == "disabled_next_and_last"
    assert not parse_list(html_page(list_html(), LIST_URL)).pagination.is_last_page


@pytest.mark.parametrize(
    "marker",
    [
        '<span class="p_next"><a>下页</a></span>',
        '<span class="p_next"><a href="javascript:next()">下页</a></span>',
        '<span class="p_next"><a href="https://other.example.org/tzgg/xstz/2.htm">下页</a></span>',
    ],
)
def test_bad_next_page_is_not_treated_as_terminal(marker):
    pagination = LIST_PAGINATION.replace(
        '<span class="p_next"><a href="xstz/800.htm">下页</a></span>', marker
    )
    assert_error(
        parse_list,
        html_page(list_html(extra=pagination), LIST_URL),
        "invalid_field",
        "next_page_url",
    )


@pytest.mark.parametrize(
    ("html", "code", "field"),
    [
        (
            '<ul class="am-list"><li><a href="/info/1517/1.htm">错误页链接</a></li></ul>',
            "missing_structure",
            "list",
        ),
        ('<div class="list_txt"></div>', "missing_structure", "entries"),
        ('<div class="list_txt"><ul class="am-list"></ul></div>', "empty_list", "entries"),
        (list_html(title="  "), "invalid_field", "title"),
        (list_html(day="2026-02-30"), "invalid_field", "published_date"),
        (list_html(day="2026-9-30"), "invalid_field", "published_date"),
        (list_html().replace("<span>学生通知</span>", ""), "missing_structure", "title"),
        (list_html().replace("<i>2026-09-30</i>", ""), "missing_structure", "published_date"),
        (
            list_html().replace('<a href="../info/1517/128231.htm">', "<a>"),
            "invalid_field",
            "detail_url",
        ),
    ],
)
def test_list_required_structure_and_fields(html, code, field):
    assert_error(parse_list, html_page(html, LIST_URL), code, field)


def test_one_bad_real_row_fails_whole_page_with_position():
    page = fixture_page("student-notices-page1.html", LIST_URL)
    broken = altered(page, lambda tree: tree.select(".list_txt li i")[7].clear())
    assert_error(parse_list, broken, "invalid_field", "published_date", 7)


@pytest.mark.parametrize(
    ("name", "url", "identity", "title", "day", "attachment_count"),
    [
        (
            "current-notice-detail.html",
            NOTICE_URL,
            "1517:128231",
            "关于2026-2027学年第一学期2026级新生选课的通知",
            date(2026, 9, 4),
            2,
        ),
        (
            "legacy-notice-detail.html",
            LEGACY_URL,
            "1517:127581",
            "关于2026-2027学年第一学期选课的通知",
            date(2026, 7, 8),
            3,
        ),
    ],
)
def test_real_notices(name, url, identity, title, day, attachment_count):
    result = parse_notice(fixture_page(name, url))
    assert result.source_document_id == identity
    assert result.parser_version == PARSER_VERSION
    assert result.content.title == title
    assert result.content.published_date == day
    assert "2026-2027" in result.content.body_text
    assert "68756893" in result.content.body_text
    tree = BeautifulSoup(result.content.body_html, "html.parser")
    assert tree.select("p")
    assert not tree.select("script, style, .fj")
    assert "已下载" not in result.content.body_text
    assert len(result.content.attachments) == attachment_count
    assert all(item.access == "not_checked" for item in result.content.attachments)
    first = result.content.attachments[0]
    assert first.name == "附件1.学生选课操作指南.pdf"
    assert first.url.host == "uc.whu.edu.cn"
    expected_id = (
        "19CF83492EE199703E92F6DE5149E482"
        if identity.endswith("128231")
        else "F402E52FEEF5A6E2B2AB9D4C721DF4D0"
    )
    assert first.source_attachment_id == f"1407739091:{expected_id}"


def test_legacy_timetable_image_is_preserved_and_resolved_in_html():
    content = parse_notice(fixture_page("legacy-notice-detail.html", LEGACY_URL)).content
    assert len(content.images) == 1
    expected = (
        "https://uc.whu.edu.cn/__local/4/13/FA/2A2CA9D19C470CDBD92584C5D85_9F0D2438_1E4B3.png"
    )
    assert str(content.images[0].url) == expected
    assert BeautifulSoup(content.body_html, "html.parser").img["src"] == expected
    assert "orisrc" not in content.body_html


def test_body_links_images_tables_and_non_http_policy():
    body = (
        '<p>请查看 <a href="../1517/2.htm">站内通知</a>，'
        '<a href="https://outside.example.org/a">外部说明</a>。</p>'
        '<p><a href="#section">锚点</a><a href="mailto:a@example.org">邮件</a>'
        '<a href="javascript:open()">脚本链接</a><a href="tel:123">电话</a></p>'
        "<table><tr><th>日期</th><th>人数</th></tr><tr><td>2026-09-30</td><td>25</td></tr></table>"
        '<p><img src="../../images/time.png" alt="时间表"></p>'
    )
    result = parse_notice(html_page(notice_html(body))).content
    assert [str(link.url) for link in result.links] == [
        "https://uc.whu.edu.cn/info/1517/2.htm",
        "https://outside.example.org/a",
    ]
    assert [link.text for link in result.links] == ["站内通知", "外部说明"]
    assert str(result.images[0].url) == "https://uc.whu.edu.cn/images/time.png"
    tree = BeautifulSoup(result.body_html, "html.parser")
    assert len(tree.select("a[href]")) == 2
    assert [a.text for a in tree.select("a:not([href])")] == ["锚点", "邮件", "脚本链接", "电话"]
    assert tree.table and tree.select("th, td")
    assert "2026-09-30\t25" in result.body_text


@pytest.mark.parametrize(
    ("html", "code", "field"),
    [
        ("<html><h1>服务暂不可用</h1><p>请稍后重试</p></html>", "missing_structure", "notice"),
        (
            notice_html().replace('class="title_nei"', 'class="unknown"'),
            "missing_structure",
            "header",
        ),
        (notice_html().replace("<b>学生通知</b>", ""), "missing_structure", "title"),
        (notice_html(title="  "), "invalid_field", "title"),
        (notice_html(day="时间：2026-02-30"), "invalid_field", "published_date"),
        (notice_html(day="发布时间未知"), "invalid_field", "published_date"),
        (
            notice_html().replace("<i>时间：2026-09-30</i>", ""),
            "missing_structure",
            "published_date",
        ),
        (notice_html().replace('id="vsb_content"', 'id="other"'), "missing_structure", "body"),
        (
            notice_html().replace('class="v_news_content"', 'class="other"'),
            "missing_structure",
            "body",
        ),
        (notice_html('<img src="javascript:bad()">'), "invalid_field", "image_url"),
    ],
)
def test_notice_invalid_structure_or_field(html, code, field):
    assert_error(parse_notice, html_page(html), code, field)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "<p>&nbsp; \n</p>",
        "<p><br><hr></p>",
        '<script>document.write("有效通知")</script><style>.x { color:red }</style>',
        "<!-- 有效通知 --><div hidden>隐藏标题</div>",
        '<p><span id="nattach123">12345</span></p>',
        '<img src="/counter.gif" width="1" height="1">',
    ],
)
def test_meaningless_body(body):
    assert_error(parse_notice, html_page(notice_html(body)), "meaningless_body", "body")


def test_image_only_body_and_normal_notice_words_are_valid():
    content = parse_notice(
        html_page(notice_html('<p><img src="/calendar.png" alt="选课时间"></p>'))
    ).content
    assert content.body_text == ""
    assert len(content.images) == 1
    assert "calendar.png" in content.body_html
    assert parse_notice(
        html_page(notice_html("<p>登录系统遇到错误时，请联系老师。</p>"))
    ).content.body_text


@pytest.mark.parametrize(("raw", "code"), [(b" \n\t", "empty_page"), (b"\xff", "invalid_utf8")])
def test_page_input_errors_for_both_parsers(raw, code):
    for parser, url in [(parse_list, LIST_URL), (parse_notice, NOTICE_URL)]:
        assert_error(parser, PageInput(content=raw, page_url=url), code)
    with pytest.raises(ValidationError):
        PageInput(content=b"", page_url=NOTICE_URL)


@pytest.mark.parametrize(
    ("name", "url", "parser"),
    [
        ("student-notices-page1.html", LIST_URL, parse_list),
        ("student-notices-page2.html", "https://uc.whu.edu.cn/tzgg/xstz/23.htm", parse_list),
        ("current-notice-detail.html", NOTICE_URL, parse_notice),
        ("legacy-notice-detail.html", LEGACY_URL, parse_notice),
    ],
)
def test_repeat_determinism_and_failure_does_not_change_future_calls(name, url, parser):
    valid = fixture_page(name, url)
    first = parser(valid)
    invalid = html_page("<html><h1>服务不可用</h1></html>", url)
    for _ in range(2):
        assert_error(parser, invalid, ParseErrorCode.MISSING_STRUCTURE)
    second = parser(valid)
    assert first == second == parser(valid)
    if parser is parse_notice:
        assert first.content.content_sha256() == second.content.content_sha256()


def test_dynamic_stats_and_outside_changes_not_content_changes():
    page = fixture_page("current-notice-detail.html", NOTICE_URL)
    original = parse_notice(page).content

    def change(tree):
        tree.title.string = "外部页面标题改变"
        tree.select_one(".fj li span").string = "999999"
        body = tree.select_one(".v_news_content")
        body.append(
            BeautifulSoup(
                '<span id="nattach999">98765</span><script>counter=54321</script>', "html.parser"
            )
        )
        tree.select_one("body").append(
            BeautifulSoup("<footer>变更的页脚 456</footer>", "html.parser")
        )

    changed = altered(page, change)
    assert hashlib.sha256(page.content).digest() != hashlib.sha256(changed.content).digest()
    assert parse_notice(changed).content.content_sha256() == original.content_sha256()
    real_change = altered(
        page, lambda tree: tree.select_one(".v_news_content p").append("报名截止调整至10月1日")
    )
    assert parse_notice(real_change).content.content_sha256() != original.content_sha256()


def test_inline_fragments_do_not_split_words_or_numbers():
    content = parse_notice(
        html_page(notice_html("<p>202<span>6</span>-202<span>7</span>学年</p>"))
    ).content
    assert content.body_text == "2026-2027学年"


def test_parser_performs_no_storage_or_database_io(monkeypatch):
    pages = [
        fixture_page("student-notices-page1.html", LIST_URL),
        fixture_page("legacy-notice-detail.html", LEGACY_URL),
    ]

    def forbidden(*args, **kwargs):
        raise AssertionError("Parser must not access files or database")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", forbidden)
    assert len(parse_list(pages[0]).entries) == 25
    assert parse_notice(pages[1]).source_document_id == "1517:127581"


def test_block_formatting_whitespace_is_normalized_but_pre_and_inline_spacing_survive():
    first = parse_notice(html_page(notice_html("<p>报名  时间</p><p>人数 25</p>"))).content
    second = parse_notice(
        html_page(notice_html("\n <p>\n报名\t 时间 \n</p>\n <p> 人数 25 </p>\n"))
    ).content
    assert first.content_sha256() == second.content_sha256()
    assert first.body_text == "报名 时间\n人数 25"
    spaced = parse_notice(
        html_page(notice_html("<p><span>Hello</span> <span>world</span></p>"))
    ).content
    assert spaced.body_text == "Hello world"
    pre = parse_notice(html_page(notice_html("<pre>if x:\n    y = 25</pre>"))).content
    assert "\n    y = 25" in BeautifulSoup(pre.body_html, "html.parser").pre.get_text()


@pytest.mark.parametrize(
    "change",
    [
        lambda tree: tree.title.replace_with(
            BeautifulSoup("<title>页外标题变化</title>", "html.parser").title
        ),
        lambda tree: tree.select_one(".fj li span").replace_with(
            BeautifulSoup('<span id="nattach15445258">123456</span>', "html.parser").span
        ),
        lambda tree: tree.select_one("body").append(
            BeautifulSoup("<footer>456 新页脚</footer>", "html.parser")
        ),
    ],
)
def test_each_outside_change_alters_raw_digest_only(change):
    page = fixture_page("current-notice-detail.html", NOTICE_URL)
    changed = altered(page, change)
    assert hashlib.sha256(page.content).digest() != hashlib.sha256(changed.content).digest()
    assert parse_notice(changed).content == parse_notice(page).content


@pytest.mark.parametrize("attr", ["hidden", 'style="display: none"', 'aria-hidden="true"'])
def test_hidden_body_is_not_success(attr):
    html = notice_html().replace('class="v_news_content"', f'class="v_news_content" {attr}')
    assert_error(parse_notice, html_page(html), "meaningless_body", "body")


@pytest.mark.parametrize(
    ("replacement", "code", "field"),
    [
        ("<a>说明.pdf</a>", "invalid_field", "attachment_url"),
        (
            '<a href="/system/_content/download.jsp?owner=123">说明.pdf</a>',
            "invalid_field",
            "attachment_identity",
        ),
        (
            '<a href="/system/_content/download.jsp?owner=123&wbfileid=A&wbfileid=B">说明.pdf</a>',
            "ambiguous_identity",
            "attachment_identity",
        ),
        (
            '<a href="/system/_content/download.jsp?owner=123&wbfileid=A"> </a>',
            "invalid_field",
            "attachment_name",
        ),
    ],
)
def test_malformed_attachment_is_not_silently_lost(replacement, code, field):
    html = notice_html().replace(
        "</div></div></div>",
        "</div></div>" + '<div class="fj"><ul><li>' + replacement + "</li></ul></div></div>",
    )
    assert_error(parse_notice, html_page(html), code, field, 0)


def test_attachment_query_order_and_unknown_query_parameters():
    link = (
        '<div class="fj"><ul><li><a href="/system/_content/download.jsp?'
        'unused=1&wbfileid=ABC123&owner=1407739091">安排.pdf</a>已下载999次</li></ul></div>'
    )
    html = notice_html().replace("</div></div></div>", "</div></div>" + link + "</div>")
    attachment = parse_notice(html_page(html)).content.attachments[0]
    assert attachment.source_attachment_id == "1407739091:ABC123"
    assert attachment.access == "not_checked"


def test_duplicate_required_structure_is_explicit_failure():
    html = notice_html().replace("<b>学生通知</b>", "<b>学生通知</b><b>另一标题</b>")
    assert_error(parse_notice, html_page(html), "missing_structure", "title")
    html = list_html().replace(
        "<span>学生通知</span>", "<span>学生通知</span><span>另一标题</span>"
    )
    assert_error(parse_list, html_page(html, LIST_URL), "missing_structure", "title", 0)


def test_programming_errors_are_not_relabelled_as_content_failures(monkeypatch):
    from signalnest import parsing

    def broken(*args):
        raise RuntimeError("programming defect")

    monkeypatch.setattr(parsing, "html_tree", broken)
    for parser, url in [(parse_list, LIST_URL), (parse_notice, NOTICE_URL)]:
        with pytest.raises(RuntimeError, match="programming defect"):
            parser(html_page(notice_html(), url))


@pytest.mark.parametrize("href", ["https://", "https:/broken", "//", "http:"])
def test_malformed_absolute_reference_is_not_resolved_as_current_page(href):
    assert_error(parse_list, html_page(list_html(href), LIST_URL), "invalid_field", "detail_url", 0)
    assert_error(
        parse_notice,
        html_page(notice_html(f'<p><a href="{href}">说明</a></p>')),
        "invalid_field",
        "body_link",
    )
