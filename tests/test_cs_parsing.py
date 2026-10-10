"""Selected sanitized CS fixtures and explicitly synthetic damage, all offline."""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError

from signalnest.contracts import PageInput
from signalnest.cs_parsing import LIST_URL, PARSER_VERSION, parse_cs_list, parse_cs_notice
from signalnest.parsing import ParseError, ParseErrorCode, parse_list, parse_notice

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures/ta-entrypoints-20261009"
HOME = "cs-undergrad-list-home"
SECOND = "cs-undergrad-list-page2"
LAST = "cs-undergrad-list-last"
NOTICE = "cs-ta-recruit-64481"
NOTICE_URL = "https://cs.whu.edu.cn/info/1074/64481.htm"


def capture(stem, url=None):
    metadata = json.loads((FIXTURES / f"{stem}.json").read_bytes())
    return PageInput(
        content=(FIXTURES / f"{stem}.html").read_bytes(),
        page_url=url or metadata["final_url"],
    )


def changed(page, edit):
    tree = BeautifulSoup(page.content.decode("utf-8"), "html.parser")
    edit(tree)
    return PageInput(content=str(tree).encode("utf-8"), page_url=page.page_url)


def assert_error(parser, page, code, field=None, index=None):
    with pytest.raises(ParseError) as caught:
        parser(page)
    assert caught.value.code == code
    if field is not None:
        assert caught.value.field == field
    if index is not None:
        assert caught.value.item_index == index
    assert len(str(caught.value)) < 160
    assert "<" not in str(caught.value) and "https://" not in str(caught.value)


def synthetic_notice(body="<p>即日起招募本科课程助教。</p>", day="2026-07-13"):
    return PageInput(
        content=(
            '<div class="article"><form name="_newscontent_fromname">'
            '<h2 class="title">计算机学院课程助教招募</h2>'
            f'<h4 class="information">发布时间：{day} 浏览量：999次</h4>'
            '<div class="content"><div id="vsb_content">'
            f'<div class="v_news_content">{body}</div></div></div></form></div>'
        ).encode(),
        page_url=NOTICE_URL,
    )


@pytest.mark.parametrize("stem", [HOME, SECOND, LAST, NOTICE])
def test_real_capture_hash_length_and_utf8(stem):
    page = capture(stem)
    metadata = json.loads((FIXTURES / f"{stem}.json").read_bytes())
    assert hashlib.sha256(page.content).hexdigest() == metadata["sha256"]
    assert len(page.content) == metadata["body_bytes"]
    assert metadata["status_code"] == 200 and metadata["body_complete"]
    page.content.decode("utf-8")


@pytest.mark.parametrize(
    "stem,count,current,total,next_url,first_id,last_id",
    [
        (HOME, 15, 1, 4, "https://cs.whu.edu.cn/xwdt/tzgg/bkjx/3.htm", "1074:66441", "1074:55001"),
        (
            SECOND,
            15,
            2,
            4,
            "https://cs.whu.edu.cn/xwdt/tzgg/bkjx/2.htm",
            "1074:54341",
            "1074:40441",
        ),
        (LAST, 13, 4, 4, None, "1074:4141", "1074:3865"),
    ],
)
def test_real_list_rows_dates_identity_relative_routes_and_visible_pages(
    stem, count, current, total, next_url, first_id, last_id
):
    result = parse_cs_list(capture(stem))
    assert result.row_count == len(result.entries) == count
    assert result.references == ()
    assert result.entries[0].source_document_id == first_id
    assert result.entries[-1].source_document_id == last_id
    assert result.pagination.current_page == current
    assert result.pagination.total_pages == total
    assert result.pagination.is_last_page == (current == total)
    assert (str(result.next_page_url) if result.next_page_url else None) == next_url
    assert result.pagination.terminal_evidence == (
        "disabled_next_and_last" if current == total else None
    )
    if current != total:
        assert str(result.pagination.last_page_url) == "https://cs.whu.edu.cn/xwdt/tzgg/bkjx/1.htm"
    assert all(entry.detail_url.host == "cs.whu.edu.cn" for entry in result.entries)
    assert all(entry.title != "武汉大学计算机学院" for entry in result.entries)
    if stem == HOME:
        first = result.entries[0]
        assert first.title == "关于计算机学院2025级计算机科学与技术试验班（雷军班） 补充选拔的通知"
        assert first.published_date == date(2026, 9, 29)
        assert str(first.detail_url) == (
            "https://cs.whu.edu.cn/content.jsp?urltype=news.NewsContentUrl"
            "&wbtreeid=1074&wbnewsid=66441"
        )
        recruit = next(
            entry for entry in result.entries if entry.source_document_id == "1074:64481"
        )
        assert recruit.published_date == date(2026, 7, 13)
        assert str(recruit.detail_url) == NOTICE_URL
    if stem == LAST:
        assert result.entries[-1].published_date == date(2022, 4, 27)


def test_short_nonterminal_page_does_not_invent_end_or_discard_old_dates():
    page = changed(capture(HOME), lambda tree: tree.select(".under-new > ul > li")[0].decompose())
    result = parse_cs_list(page)
    assert len(result.entries) == 14 and result.next_page_url is not None
    assert not result.pagination.is_last_page
    assert result.entries[-1].published_date == date(2025, 7, 10)


def test_total_is_from_declarations_not_four_or_route_arithmetic():
    def edit(tree):
        panel = tree.select_one(".p_pages")
        old_last_number = panel.select(".p_no")[-1]
        old_last_number.insert_after(
            BeautifulSoup('<span class="p_no"><a href="bkjx/9.htm">5</a></span>', "html.parser")
        )
        panel.select_one(".p_last a")["href"] = "bkjx/9.htm"

    result = parse_cs_list(changed(capture(HOME), edit))
    assert result.pagination.total_pages == 5
    assert result.pagination.current_page == 1
    assert str(result.pagination.last_page_url).endswith("/9.htm")
    assert str(result.next_page_url).endswith("/3.htm")


@pytest.mark.parametrize(
    "selector,field",
    [
        ("div.study.under-new", "list"),
        (".under-new > ul", "entries"),
        (".pagebar", "pagination"),
        (".p_pages", "pagination"),
        (".p_no_d", "current_page"),
        (".p_first_d", "first_page_url"),
        (".p_prev_d", "prev_page_url"),
        (".p_next", "next_page_url"),
        (".p_last", "last_page_url"),
    ],
)
@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_and_duplicate_required_list_nodes_fail(selector, field, duplicate):
    def edit(tree):
        node = tree.select_one(selector)
        if duplicate:
            node.insert_after(BeautifulSoup(str(node), "html.parser"))
        else:
            node.decompose()

    assert_error(parse_cs_list, changed(capture(HOME), edit), "missing_structure", field)


@pytest.mark.parametrize(
    "selector,attribute,value,code",
    [
        (".p_no_d", "text", "0", "invalid_field"),
        (".p_no_d", "text", "5", "invalid_pagination"),
        (".p_no_d", "text", "1/4", "invalid_field"),
        (".p_no", "text", "2", "missing_structure"),
        (".p_next", "class", "p_next p_next_d", "invalid_pagination"),
        (".p_next", "class", "p_next p_fun_d", "invalid_pagination"),
        (".p_next a", "text", "下页", "invalid_pagination"),
        (".p_next a", "href", "https://cs.whu.edu.cn/xwdt/tzgg/bkjx.htm", "invalid_pagination"),
        (".p_next a", "href", "https://uc.whu.edu.cn/tzgg/xstz.htm", "invalid_field"),
        (".p_next a", "href", "https://cs.whu.edu.cn/xwdt/tzgg/bkxg.htm", "invalid_field"),
        (".p_next a", "href", "bkjx/3.htm?unused=1", "invalid_field"),
        (".p_next a", "href", "bkjx/3.htm#", "invalid_field"),
        (".p_next a", "href", "http://cs.whu.edu.cn/xwdt/tzgg/bkjx/3.htm", "invalid_field"),
        (".p_next a", "href", "https://cs.whu.edu.cn:444/xwdt/tzgg/bkjx/3.htm", "invalid_field"),
        (".p_next a", "href", "javascript:void(0)", "invalid_field"),
        (".p_next a", "href", "http://", "invalid_field"),
        (".p_next a", "href", "bkjx/2.htm", "invalid_pagination"),
        (".p_last a", "href", "bkjx/2.htm", "invalid_pagination"),
        (".p_pages", "hidden", "", "invalid_pagination"),
        (".p_no_d", "style", "display:none", "invalid_pagination"),
    ],
)
def test_damaged_pagination_never_reports_success(selector, attribute, value, code):
    def edit(tree):
        node = tree.select_one(selector)
        if attribute == "text":
            node.string = value
        elif attribute == "class":
            node[attribute] = value.split()
        else:
            node[attribute] = value

    assert_error(parse_cs_list, changed(capture(HOME), edit), code)


@pytest.mark.parametrize("control", ["next", "last"])
def test_terminal_disabled_controls_cannot_have_links_or_conflicting_classes(control):
    def linked(tree):
        tree.select_one(f".p_{control}_d").append(
            BeautifulSoup('<a href="2.htm">下一页</a>', "html.parser")
        )

    assert_error(parse_cs_list, changed(capture(LAST), linked), "invalid_pagination")

    def active(tree):
        tree.select_one(f".p_{control}_d")["class"] = [f"p_{control}", "p_fun"]

    assert_error(parse_cs_list, changed(capture(LAST), active), "missing_structure")


def test_conflicting_first_prev_number_targets_and_visible_gaps_fail():
    for selector, href in ((".p_first a", "2.htm"), (".p_prev a", "1.htm")):
        assert_error(
            parse_cs_list,
            changed(
                capture(SECOND),
                lambda tree, selector=selector, href=href: tree.select_one(selector).__setitem__(
                    "href", href
                ),
            ),
            "invalid_pagination",
        )
    assert_error(
        parse_cs_list,
        changed(capture(HOME), lambda tree: tree.select(".p_no")[0].decompose()),
        "invalid_pagination",
    )
    assert_error(
        parse_cs_list,
        changed(
            capture(HOME),
            lambda tree: tree.select(".p_no a")[1].__setitem__("href", "bkjx/3.htm"),
        ),
        "invalid_pagination",
    )


@pytest.mark.parametrize(
    "selector,field", [("p", "title"), ("span", "published_date"), ("a", "entry")]
)
def test_bad_necessary_row_is_whole_page_failure(selector, field):
    def edit(tree):
        tree.select(".under-new > ul > li")[3].select_one(selector).decompose()

    assert_error(parse_cs_list, changed(capture(HOME), edit), "missing_structure", field, 3)


@pytest.mark.parametrize(
    "href", ["#x", "mailto:a@example.org", "javascript:void(0)", "http://", "a%ZZ", "a\\b", "a b"]
)
def test_invalid_list_reference_is_not_silently_registered(href):
    def edit(tree):
        tree.select(".under-new > ul > li > a")[2]["href"] = href

    assert_error(parse_cs_list, changed(capture(HOME), edit), "invalid_field", "detail_url", 2)


def test_unadapted_valid_references_use_existing_contract_and_order():
    def edit(tree):
        rows = tree.select(".under-new > ul > li > a")
        rows[1]["href"] = "https://other.example.org/opportunity?id=1&x=2"
        rows[4]["href"] = "../../info/1075/123.htm"
        rows[6]["href"] = "../../other.htm"

    result = parse_cs_list(changed(capture(HOME), edit))
    assert len(result.entries) == 12 and len(result.references) == 3
    assert result.row_count == 15
    assert [item.row_index for item in result.references] == [1, 4, 6]
    assert [item.reference_kind for item in result.references] == [
        "external",
        "unsupported_column",
        "unsupported_route",
    ]
    assert result.ordered_rows()[4] == result.references[1]
    assert len({item.candidate_key() for item in result.references}) == 3
    assert result.next_page_url is not None


def test_real_detail_preserves_conflicts_plain_text_channels_and_structure():
    result = parse_cs_notice(capture(NOTICE))
    content = result.content
    assert result.source_document_id == "1074:64481"
    assert result.parser_version == PARSER_VERSION == "cs-undergrad-notices-v1"
    assert str(result.page_url) == NOTICE_URL
    assert content.title == "2026-2027学年第一学期计算机学院助教招募通知"
    assert content.published_date == date(2026, 7, 13)
    for phrase in (
        "高年级本科生中招募本科课程助教",
        "面向全院研究生招募本科课程助教",
        "其它特别优秀的高年级本科生",
        "获得85分以上成绩",
        "考查同意",
        "9月20日前填写在线文档报名",
        "9月20日前交老师签字",
        "2025年7月10日",
        "https://docs.qq.com/sheet/REDACTED",
        "两个表都在QQ群文件里",
    ):
        assert phrase in content.body_text
    assert "<p" in content.body_html and "<span" in content.body_html
    assert "\n一、助教岗位的设置\n" in content.body_text
    assert "\n四、助教工作报名流程\n" in content.body_text
    assert "浏览量" not in content.body_text and "_showDynClicks" not in content.body_html
    # The capture has plain text, not an a/img/download template. No fake file or link.
    assert content.links == content.images == content.attachments == ()


@pytest.mark.parametrize(
    "url",
    [
        NOTICE_URL,
        NOTICE_URL + "?unused=1",
        NOTICE_URL + "?wbnewsid=64481&wbtreeid=1074",
        "https://cs.whu.edu.cn/content.jsp?urltype=news.NewsContentUrl&wbtreeid=1074&wbnewsid=64481",
        "https://cs.whu.edu.cn/content.jsp?unused=1&wbnewsid=64481&wbtreeid=1074",
    ],
)
def test_static_and_observed_jsp_identities_unify_without_network_claim(url):
    notice = parse_cs_notice(capture(NOTICE, url))
    assert notice.source_document_id == "1074:64481"
    assert notice.content == parse_cs_notice(capture(NOTICE)).content
    # The JSP replay supplies the static capture explicitly; it is not proof of
    # a successful HTTP request or that a JSP response has this template.


@pytest.mark.parametrize(
    "url,code",
    [
        ("https://uc.whu.edu.cn/info/1074/64481.htm", "unsupported_identity"),
        ("https://ems.whu.edu.cn/info/1074/64481.htm", "unsupported_identity"),
        ("http://cs.whu.edu.cn/info/1074/64481.htm", "unsupported_identity"),
        ("https://cs.whu.edu.cn:444/info/1074/64481.htm", "unsupported_identity"),
        ("https://cs.whu.edu.cn/info/1075/64481.htm", "unsupported_identity"),
        ("https://cs.whu.edu.cn/info/1074/064481.htm", "unsupported_identity"),
        (NOTICE_URL + "#body", "unsupported_identity"),
        (NOTICE_URL + "?wbnewsid=1", "ambiguous_identity"),
        (NOTICE_URL + "?wbtreeid=1074&wbtreeid=1074", "ambiguous_identity"),
        (NOTICE_URL + "?urltype=other", "unsupported_identity"),
        ("https://cs.whu.edu.cn/content.jsp?wbtreeid=1074", "invalid_field"),
        ("https://cs.whu.edu.cn/content.jsp?wbtreeid=1074&wbnewsid=", "invalid_field"),
        (
            "https://cs.whu.edu.cn/content.jsp?wbtreeid=1074&wbnewsid=1&wbnewsid=2",
            "ambiguous_identity",
        ),
        ("https://cs.whu.edu.cn/content.jsp?wbtreeid=1075&wbnewsid=1", "unsupported_identity"),
        ("https://cs.whu.edu.cn/2026/content.jsp?wbtreeid=1074&wbnewsid=1", "unsupported_identity"),
        ("https://cs.whu.edu.cn/system/resource/code/auth/caslogin.jsp", "unsupported_identity"),
    ],
)
def test_wrong_source_route_and_ambiguous_detail_identity_fail(url, code):
    assert_error(parse_cs_notice, capture(NOTICE, url), code, "identity")


@pytest.mark.parametrize(
    "href,code",
    [
        ("../../info/1074/no.htm", "unsupported_identity"),
        ("../../info/1074/64481.htm?wbnewsid=1", "ambiguous_identity"),
        ("../../content.jsp?wbtreeid=1074", "invalid_field"),
        ("../../content.jsp?wbtreeid=1074&wbnewsid=1&wbnewsid=1", "ambiguous_identity"),
    ],
)
def test_damaged_known_article_does_not_become_pending_reference(href, code):
    def edit(tree):
        tree.select_one(".under-new > ul > li > a")["href"] = href

    assert_error(parse_cs_list, changed(capture(HOME), edit), code, "identity", 0)


@pytest.mark.parametrize(
    "selector,field",
    [
        ("div.article", "notice"),
        ('form[name="_newscontent_fromname"]', "notice"),
        ("h2.title", "title"),
        ("h4.information", "published_date"),
        ("form > div.content", "body"),
        ("#vsb_content", "body"),
        (".v_news_content", "body"),
    ],
)
@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_ambiguous_detail_nodes_fail(selector, field, duplicate):
    def edit(tree):
        node = tree.select_one(selector)
        if duplicate:
            node.insert_after(BeautifulSoup(str(node), "html.parser"))
        else:
            node.decompose()

    assert_error(parse_cs_notice, changed(capture(NOTICE), edit), "missing_structure", field)


@pytest.mark.parametrize("day", ["2026-02-30", "2026/07/13", "07-13-2026", ""])
def test_visible_date_is_not_guessed_from_body_signature(day):
    assert_error(parse_cs_notice, synthetic_notice(day=day), "invalid_field", "published_date")


@pytest.mark.parametrize(
    "selector,field", [("h2.title", "title"), ("h4.information", "published_date")]
)
def test_hidden_required_headers_cannot_support_visible_content(selector, field):
    assert_error(
        parse_cs_notice,
        changed(capture(NOTICE), lambda tree: tree.select_one(selector).__setitem__("hidden", "")),
        "invalid_field",
        field,
    )


@pytest.mark.parametrize(
    "body",
    [
        "",
        "\n  ",
        "<script>招聘</script>",
        "<div hidden>招聘</div>",
        '<img src="/pixel.gif" width="1" height="1">',
    ],
)
def test_meaningless_body_fails(body):
    assert_error(parse_cs_notice, synthetic_notice(body=body), "meaningless_body", "body")


def test_synthetic_web_references_images_and_body_download_reference_are_not_fetched():
    body = (
        '<p><a href="../apply.htm">报名</a><a href="https://other.example.org/apply">外部申请</a>'
        '<a href="#body">定位</a><a href="mailto:office@example.org">邮件</a>'
        '<a href="javascript:void(0)">菜单</a></p><img src="../../images/poster.png" alt="海报">'
        '<img src="/pixel.gif" width="1" height="1">'
        '<p><a href="/system/_content/download.jsp?owner=123&wbfileid=ABC">申请表.doc</a></p>'
    )
    content = parse_cs_notice(synthetic_notice(body=body)).content
    assert [str(link.url) for link in content.links][:2] == [
        "https://cs.whu.edu.cn/info/apply.htm",
        "https://other.example.org/apply",
    ]
    assert (
        len(content.images) == 1
        and str(content.images[0].url) == "https://cs.whu.edu.cn/images/poster.png"
    )
    assert content.images[0].alt_text == "海报"
    assert len(content.attachments) == 1
    assert content.attachments[0].source_attachment_id == "123:ABC"
    assert content.attachments[0].access == "not_checked"
    assert content.attachments[0].name == "申请表.doc"
    assert 'href="#body"' not in content.body_html
    assert "javascript:" not in content.body_html and "mailto:" not in content.body_html
    assert "菜单" in content.body_text
    image_only = parse_cs_notice(synthetic_notice(body='<img src="/poster.png">')).content
    assert image_only.body_text == "" and len(image_only.images) == 1


@pytest.mark.parametrize(
    "href",
    [
        "/system/_content/download.jsp?owner=1",
        "/system/_content/download.jsp?owner=1&wbfileid=ABC&wbfileid=DEF",
    ],
)
def test_synthetic_damaged_download_reference_fails(href):
    assert_error(
        parse_cs_notice,
        synthetic_notice(body=f'<p><a href="{href}">申请表.doc</a></p>'),
        "ambiguous_identity" if "DEF" in href else "invalid_field",
        "attachment_identity",
    )


def test_determinism_stats_outside_area_and_real_content_change():
    page = capture(NOTICE)
    notice = parse_cs_notice(page)
    assert parse_cs_notice(page) == notice

    def stats(tree):
        tree.select_one("h4.information").string = "发布时间：2026-07-13 浏览量：99999次"
        tree.select_one(".v_news_content").append(
            BeautifulSoup(
                '<span id="nattach123">99999</span><script>counter()</script>', "html.parser"
            )
        )
        tree.select_one("title").string = "不同的学院导航区域"
        tree.select_one(".page-dow").string = "另一个上页链接"

    modified = changed(page, stats)
    assert hashlib.sha256(modified.content).hexdigest() != hashlib.sha256(page.content).hexdigest()
    assert parse_cs_notice(modified).content.content_sha256() == notice.content.content_sha256()
    assert parse_cs_list(capture(HOME)) == parse_cs_list(capture(HOME))

    def content_change(tree):
        tree.select_one(".v_news_content").append(
            BeautifulSoup("<p>新增课程成绩要求为90分。</p>", "html.parser")
        )

    assert (
        parse_cs_notice(changed(page, content_change)).content.content_sha256()
        != notice.content.content_sha256()
    )


@pytest.mark.parametrize("parser,url", [(parse_cs_list, LIST_URL), (parse_cs_notice, NOTICE_URL)])
@pytest.mark.parametrize(
    "body,code",
    [
        (b"\xff", "invalid_utf8"),
        (b"  \n", "empty_page"),
        (b"<html><h1>login</h1></html>", "missing_structure"),
    ],
)
def test_expected_input_failures_are_repeatable_and_do_not_poison_success(parser, url, body, code):
    page = PageInput(content=body, page_url=url)
    for _ in range(2):
        assert_error(parser, page, code)
    assert parser(capture(HOME if parser is parse_cs_list else NOTICE))


def test_empty_bytes_keep_shared_contract_and_cs_does_not_expand_uc_parser():
    with pytest.raises(ValidationError):
        PageInput(content=b"", page_url=LIST_URL)
    assert_error(parse_notice, capture(NOTICE), ParseErrorCode.UNSUPPORTED_IDENTITY)
    assert_error(parse_list, capture(HOME), ParseErrorCode.MISSING_STRUCTURE)
