"""Local WHU evidence, including the actual 13-entry terminal page. No traversal."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from signalnest.contracts import PageInput
from signalnest.parsing import PARSER_VERSION, ParseError, parse_list, parse_notice

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures"
HOME_URL = "https://uc.whu.edu.cn/tzgg/xstz.htm"
MIDDLE_URL = "https://uc.whu.edu.cn/tzgg/xstz/23.htm"
LAST_URL = "https://uc.whu.edu.cn/tzgg/xstz/1.htm"
HOME = "ingestion/student-notices-home-20261001.html"
LAST = "ingestion/student-notices-last-20261001.html"


def fixture(name=HOME, url=HOME_URL):
    return PageInput(content=(FIXTURES / name).read_bytes(), page_url=url)


def changed(name, url, mutate):
    tree = BeautifulSoup(fixture(name, url).content.decode("utf-8"), "html.parser")
    mutate(tree)
    return PageInput(content=str(tree).encode("utf-8"), page_url=url)


def check_error(page, code, field):
    with pytest.raises(ParseError) as caught:
        parse_list(page)
    assert caught.value.code == code
    assert caught.value.field == field
    assert len(str(caught.value)) < 150
    assert "<html" not in str(caught.value)


@pytest.mark.parametrize(
    "name,url,current,count,next_url,last_url,evidence",
    [
        ("student-notices-page1.html", HOME_URL, 1, 25, MIDDLE_URL, LAST_URL, None),
        (HOME, HOME_URL, 1, 25, MIDDLE_URL, LAST_URL, None),
        (
            "student-notices-page2.html",
            MIDDLE_URL,
            2,
            25,
            "https://uc.whu.edu.cn/tzgg/xstz/22.htm",
            LAST_URL,
            None,
        ),
        (LAST, LAST_URL, 24, 13, None, None, "disabled_next_and_last"),
    ],
)
def test_real_pagination(name, url, current, count, next_url, last_url, evidence):
    result = parse_list(fixture(name, url))
    assert len(result.entries) == count
    assert len({entry.source_document_id for entry in result.entries}) == count
    assert result.pagination.current_page == current
    assert result.pagination.total_pages == 24
    assert result.pagination.is_last_page == (current == 24)
    assert result.pagination.terminal_evidence == evidence
    assert (str(result.next_page_url) if result.next_page_url else None) == next_url
    assert (
        str(result.pagination.last_page_url) if result.pagination.last_page_url else None
    ) == last_url
    assert parse_list(fixture(name, url)) == result


@pytest.mark.parametrize("name,url", [(HOME, HOME_URL), (LAST, LAST_URL)])
def test_new_fixture_metadata_is_used_as_explicit_provenance(name, url):
    metadata = json.loads((FIXTURES / name).with_suffix(".json").read_text())
    page = fixture(name, metadata["final_url"])
    assert str(page.page_url) == url
    assert len(page.content) == metadata["bytes"]
    assert hashlib.sha256(page.content).hexdigest() == metadata["sha256"]
    assert metadata["status_code"] == 200
    assert parse_list(page).pagination.is_last_page == (name == LAST)


@pytest.mark.parametrize(
    "selector,field",
    [
        (".page", "pagination"),
        (".p_pages", "pagination"),
        (".p_no_d", "current_page"),
        (".p_next", "next_page_url"),
        (".p_last", "last_page_url"),
    ],
)
@pytest.mark.parametrize("operation", ["delete", "duplicate"])
def test_required_pagination_nodes_cannot_be_missing_or_duplicated(selector, field, operation):
    def mutate(tree):
        node = tree.select_one(selector)
        if operation == "delete":
            node.decompose()
        else:
            node.insert_after(copy.deepcopy(node))

    check_error(changed(HOME, HOME_URL, mutate), "missing_structure", field)


@pytest.mark.parametrize(
    "selector,field",
    [
        (".p_next_d", "next_page_url"),
        (".p_last_d", "last_page_url"),
    ],
)
@pytest.mark.parametrize("operation", ["delete", "duplicate", "anchor"])
def test_terminal_controls_require_unique_disabled_markers(selector, field, operation):
    def mutate(tree):
        node = tree.select_one(selector)
        if operation == "delete":
            node.decompose()
        elif operation == "duplicate":
            node.insert_after(copy.deepcopy(node))
        else:
            anchor = tree.new_tag("a", href=HOME_URL)
            anchor.string = node.get_text()
            node.clear()
            node.append(anchor)

    check_error(
        changed(LAST, LAST_URL, mutate),
        "invalid_pagination" if operation == "anchor" else "missing_structure",
        field,
    )


@pytest.mark.parametrize(
    "selector,classes",
    [
        (".p_next", ["p_next", "p_next_d"]),
        (".p_last", ["p_last", "p_last_d"]),
        (".p_next", ["p_next", "p_fun_d"]),
        (".p_last", ["p_last", "p_fun_d"]),
    ],
)
def test_active_disabled_class_conflicts(selector, classes):
    page = changed(
        HOME, HOME_URL, lambda tree: tree.select_one(selector).__setitem__("class", classes)
    )
    check_error(
        page, "invalid_pagination", "next_page_url" if selector == ".p_next" else "last_page_url"
    )


@pytest.mark.parametrize("selector", [".p_next", ".p_last"])
def test_disabled_control_on_nonterminal_page_is_a_conflict(selector):
    def mutate(tree):
        node = tree.select_one(selector)
        label = node.get_text()
        node.clear()
        node.string = label
        node["class"] = [selector[1:] + "_d", "p_fun_d"]

    check_error(changed(HOME, HOME_URL, mutate), "invalid_pagination", "pagination")


@pytest.mark.parametrize("selector,label", [(".p_next_d", "下页"), (".p_last_d", "尾页")])
def test_active_control_on_terminal_page_is_a_conflict(selector, label):
    def mutate(tree):
        node = tree.select_one(selector)
        node["class"] = [selector[1:-2], "p_fun"]
        node.clear()
        anchor = tree.new_tag("a", href=HOME_URL)
        anchor.string = label
        node.append(anchor)

    check_error(changed(LAST, LAST_URL, mutate), "invalid_pagination", "pagination")


@pytest.mark.parametrize("selector", [".p_next", ".p_last"])
def test_active_controls_cannot_lose_their_link(selector):
    page = changed(HOME, HOME_URL, lambda tree: tree.select_one(f"{selector} a").unwrap())
    check_error(
        page, "missing_structure", "next_page_url" if selector == ".p_next" else "last_page_url"
    )


@pytest.mark.parametrize("selector", [".p_no_d", ".p_next", ".p_last"])
def test_marker_tag_shape_cannot_be_guessed(selector):
    def mutate(tree):
        tree.select_one(selector).name = "div"

    field = {".p_no_d": "page_numbers", ".p_next": "next_page_url", ".p_last": "last_page_url"}[
        selector
    ]
    check_error(changed(HOME, HOME_URL, mutate), "invalid_pagination", field)


@pytest.mark.parametrize("value", ["", "0", "-1", "1.0", "第1页", "1/24", "01", "一", "9" * 5000])
def test_invalid_current_page_numbers(value):
    def mutate(tree):
        tree.select_one(".p_no_d").string = value

    check_error(changed(HOME, HOME_URL, mutate), "invalid_field", "current_page")


@pytest.mark.parametrize("value", ["", "0", "-24", "24/24", "二十四", "024"])
def test_invalid_total_page_labels(value):
    def mutate(tree):
        tree.select(".p_no a")[-1].string = value

    check_error(changed(HOME, HOME_URL, mutate), "invalid_field", "page_numbers")


def test_total_requires_tail_link_and_numeric_link_to_agree():
    page = changed(
        HOME, HOME_URL, lambda tree: tree.select_one(".p_last a").__setitem__("href", "xstz/22.htm")
    )
    check_error(page, "invalid_pagination", "pagination")
    page = changed(HOME, HOME_URL, lambda tree: tree.select(".p_no")[-1].decompose())
    check_error(page, "invalid_pagination", "pagination")


@pytest.mark.parametrize(
    "name,url,value", [(HOME, HOME_URL, "24"), (LAST, LAST_URL, "23"), (LAST, LAST_URL, "25")]
)
def test_current_page_must_agree_with_visible_numbers(name, url, value):
    page = changed(name, url, lambda tree: setattr(tree.select_one(".p_no_d"), "string", value))
    check_error(page, "invalid_pagination", "page_numbers")


def test_current_marker_cannot_itself_be_an_active_number_link():
    def mutate(tree):
        node = tree.select_one(".p_no_d")
        node["class"] = ["p_no", "p_no_d"]

    check_error(changed(HOME, HOME_URL, mutate), "invalid_pagination", "page_numbers")


def test_current_marker_must_not_contain_a_link():
    def mutate(tree):
        node = tree.select_one(".p_no_d")
        node.clear()
        anchor = tree.new_tag("a", href=HOME_URL)
        anchor.string = "1"
        node.append(anchor)

    check_error(changed(HOME, HOME_URL, mutate), "invalid_pagination", "current_page")


@pytest.mark.parametrize(
    "selector,label,field",
    [
        (".p_next a", "下一页", "next_page_url"),
        (".p_last a", "末页", "last_page_url"),
    ],
)
def test_control_labels_must_match_observed_source(selector, label, field):
    page = changed(HOME, HOME_URL, lambda tree: setattr(tree.select_one(selector), "string", label))
    check_error(page, "invalid_pagination", field)


@pytest.mark.parametrize("selector", [".page", ".p_pages"])
def test_nested_duplicate_pagination_is_not_ignored(selector):
    def mutate(tree):
        node = tree.select_one(selector)
        wrapper = tree.new_tag("div")
        wrapper.append(copy.deepcopy(node))
        node.parent.append(wrapper)

    check_error(changed(HOME, HOME_URL, mutate), "missing_structure", "pagination")


@pytest.mark.parametrize(
    "name,url,selector,attribute,value",
    [
        (LAST, LAST_URL, ".p_no_d", "hidden", ""),
        (LAST, LAST_URL, ".p_next_d", "aria-hidden", "true"),
        (LAST, LAST_URL, ".p_last_d", "style", "visibility:hidden"),
        (LAST, LAST_URL, ".page", "style", "display: none !important"),
        (LAST, LAST_URL, ".p_pages", "hidden", ""),
        (HOME, HOME_URL, ".p_no a", "hidden", ""),
        (HOME, HOME_URL, ".p_next a", "hidden", ""),
        (HOME, HOME_URL, ".p_last a", "aria-hidden", "true"),
        (HOME, HOME_URL, ".p_dot", "hidden", ""),
    ],
)
def test_explicitly_hidden_pagination_evidence_is_not_accepted(
    name, url, selector, attribute, value
):
    page = changed(name, url, lambda tree: tree.select_one(selector).__setitem__(attribute, value))
    check_error(page, "invalid_pagination", "pagination")
    assert parse_list(fixture(name, url)).pagination.is_last_page == (name == LAST)


def test_numeric_links_cannot_claim_the_same_target_or_the_current_page():
    def mutate(tree):
        links = tree.select(".p_no a")
        links[1]["href"] = links[0]["href"]

    check_error(changed(HOME, HOME_URL, mutate), "invalid_pagination", "page_numbers")
    page = changed(
        HOME, HOME_URL, lambda tree: tree.select_one(".p_no a").__setitem__("href", HOME_URL)
    )
    check_error(page, "invalid_pagination", "page_numbers")


@pytest.mark.parametrize(
    "href",
    [
        "",
        "#",
        "javascript:next()",
        "https://",
        "https://[invalid/",
        HOME_URL,
        "https://UC.WHU.EDU.CN:443/tzgg/xstz.htm",
        "http://uc.whu.edu.cn/tzgg/xstz/23.htm",
        "https://uc.whu.edu.cn:444/tzgg/xstz/23.htm",
        "https://other.example.org/tzgg/xstz/23.htm",
        "/tzgg/jxtz/23.htm",
        "/info/1517/23.htm",
        "/2022/show.jsp?wbtreeid=1517",
        "/tzgg/xstz/0.htm",
        "/tzgg/xstz/023.htm",
        "/tzgg/xstz/23.html",
        "xstz/23.htm?anything=1",
        "xstz/23.htm#part",
        "xstz/23.htm?",
        "xstz/23.htm#",
        "https://user:PRIVATE@uc.whu.edu.cn/tzgg/xstz/23.htm",
        "xstz\\23.htm",
        "xstz/2\n3.htm",
    ],
)
def test_next_url_source_constraints_and_self_link(href):
    page = changed(
        HOME, HOME_URL, lambda tree: tree.select_one(".p_next a").__setitem__("href", href)
    )
    check_error(page, "invalid_field", "next_page_url")


def test_visible_next_number_link_must_match_next_control():
    page = changed(
        HOME, HOME_URL, lambda tree: tree.select_one(".p_next a").__setitem__("href", "xstz/22.htm")
    )
    check_error(page, "invalid_pagination", "next_page_url")


def test_short_old_page_cannot_be_mistaken_for_terminal():
    def mutate(tree):
        entries = tree.select(".list_txt li")
        for node in entries[1:]:
            node.decompose()
        tree.select_one(".list_txt li i").string = "2010-01-01"

    result = parse_list(changed(HOME, HOME_URL, mutate))
    assert len(result.entries) == 1
    assert result.entries[0].published_date.year == 2010
    assert not result.pagination.is_last_page
    assert result.pagination.terminal_evidence is None
    assert str(result.next_page_url) == MIDDLE_URL


def test_static_filename_does_not_determine_page_number_or_total():
    tree = BeautifulSoup(fixture("student-notices-page2.html", MIDDLE_URL).content, "html.parser")
    tree.select(".p_no a")[-1].string = "37"
    tree.select_one(".p_next a")["href"] = "900.htm"
    tree.select(".p_no a")[1]["href"] = "900.htm"
    tree.select_one(".p_last a")["href"] = "700.htm"
    tree.select(".p_no a")[-1]["href"] = "700.htm"
    result = parse_list(
        PageInput(content=str(tree).encode(), page_url="https://uc.whu.edu.cn/tzgg/xstz/600.htm")
    )
    assert result.pagination.current_page == 2
    assert result.pagination.total_pages == 37
    assert str(result.next_page_url) == "https://uc.whu.edu.cn/tzgg/xstz/900.htm"
    assert str(result.pagination.last_page_url) == "https://uc.whu.edu.cn/tzgg/xstz/700.htm"


@pytest.mark.parametrize(
    "name,url,digest",
    [
        (
            "current-notice-detail.html",
            "https://uc.whu.edu.cn/info/1517/128231.htm",
            "641d653509d3ee8ee0e967cc0253dbb89e8a0c572b32e0ebecadc057a4d06f3a",
        ),
        (
            "legacy-notice-detail.html",
            "https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517&wbnewsid=127581",
            "7816f0d0bd9bf5ea5aee3b893e5ad73fda51a9d5955cc9db2b7ef1e70295d82e",
        ),
    ],
)
def test_version_bump_preserves_v1_detail_content_digest(name, url, digest):
    notice = parse_notice(fixture(name, url))
    assert notice.parser_version == PARSER_VERSION == "whu-student-notices-v3"
    assert notice.content.content_sha256() == digest
