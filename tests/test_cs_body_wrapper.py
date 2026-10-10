"""Observed CS 501 wrapper shape, using unchanged real content in memory.

The three live originals remain in the private WSL archive (response 2, 6, 7).
These are explicitly adapted existing fixture bytes, not new real captures.
"""

import pytest
from bs4 import BeautifulSoup
from test_cs_parsing import NOTICE, capture, changed

from signalnest.contracts import PageInput
from signalnest.cs_parsing import PARSER_VERSION, parse_cs_notice
from signalnest.parsing import ParseError


def wrapper_page(value="vsb_content_501"):
    return changed(
        capture(NOTICE), lambda tree: tree.select_one("#vsb_content").__setitem__("id", value)
    )


@pytest.mark.parametrize(
    "uri",
    [
        "https://cs.whu.edu.cn/info/1074/64481.htm",
        "https://cs.whu.edu.cn/content.jsp?wbtreeid=1074&wbnewsid=64481",
    ],
)
def test_observed_wrapper_has_identical_content_identity_and_digest(uri):
    original = capture(NOTICE)
    before = parse_cs_notice(original)
    adapted = wrapper_page().model_copy(update={"page_url": original.page_url})
    after = parse_cs_notice(PageInput(content=adapted.content, page_url=uri))
    assert before.parser_version == after.parser_version == PARSER_VERSION
    assert before.source_document_id == after.source_document_id == "1074:64481"
    assert before.content == after.content
    assert before.content.content_sha256() == after.content.content_sha256()
    assert original.content != adapted.content


def test_observed_wrapper_does_not_include_sibling_download_statistics():
    def edit(tree):
        tree.select_one("#vsb_content")["id"] = "vsb_content_501"
        sibling = BeautifulSoup("<p>下载次数：999 <script>dynamic()</script></p>", "html.parser")
        tree.select_one("#vsb_content_501").insert_after(sibling.p)

    result = parse_cs_notice(changed(capture(NOTICE), edit))
    assert result.content == parse_cs_notice(capture(NOTICE)).content


@pytest.mark.parametrize("value", ["vsb_content_502", "vsb_content_1", "arbitrary_content"])
def test_unobserved_wrappers_still_fail_instead_of_guessing(value):
    with pytest.raises(ParseError) as caught:
        parse_cs_notice(wrapper_page(value))
    assert caught.value.code == "missing_structure" and caught.value.field == "body"


def test_both_known_wrappers_are_ambiguous_and_fail():
    def edit(tree):
        second = BeautifulSoup(
            '<div id="vsb_content_501"><div class="v_news_content"><p>第二段正文</p></div></div>',
            "html.parser",
        )
        tree.select_one("#vsb_content").insert_after(second.div)

    with pytest.raises(ParseError) as caught:
        parse_cs_notice(changed(capture(NOTICE), edit))
    assert caught.value.code == "missing_structure" and caught.value.field == "body"


def test_observed_wrapper_with_script_only_body_still_fails():
    def edit(tree):
        tree.select_one("#vsb_content")["id"] = "vsb_content_501"
        body = tree.select_one(".v_news_content")
        body.clear()
        body.append(BeautifulSoup("<script>dynamic()</script>", "html.parser").script)

    with pytest.raises(ParseError) as caught:
        parse_cs_notice(changed(capture(NOTICE), edit))
    assert caught.value.code == "meaningless_body"
