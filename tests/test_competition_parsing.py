"""The observed competition template, without widening body or title fallbacks."""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from test_notification_service import ACTIVATED_AT, live, observation, rows, start_live
from test_notification_service import activated as activated

from signalnest.contracts import PageInput
from signalnest.ingestion import process_response
from signalnest.parsing import PARSER_VERSION, ParseError, ParseErrorCode, parse_notice
from signalnest.schema import notice_versions, notification_events, raw_responses

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "research/fixtures/notifications/notice-18135-20261005T133554Z.html"
)
METADATA = json.loads(FIXTURE.with_suffix(".json").read_text())
URL = METADATA["final_url"]


def page(content=None):
    return PageInput(content=FIXTURE.read_bytes() if content is None else content, page_url=URL)


def changed(change):
    tree = BeautifulSoup(FIXTURE.read_bytes(), "html.parser")
    change(tree)
    return page(str(tree).encode())


def test_real_competition_template_preserves_body_images_and_external_references():
    original = FIXTURE.read_bytes()
    assert hashlib.sha256(original).hexdigest() == METADATA["sha256"]
    parsed = parse_notice(page())
    assert parsed.source_document_id == "1517:18135"
    assert parsed.parser_version == PARSER_VERSION == "whu-student-notices-v3"
    content = parsed.content
    assert content.title == "首届全球数智教育创新大赛报名开启！等你来挑战！"
    assert content.published_date == date(2024, 6, 28)
    assert "高校全日制在校本科生、硕士生或博士生" in content.body_text
    assert "报名截止时间见各分赛道具体通知" in content.body_text
    assert "2024年6月底—9月上旬" in content.body_text
    assert "DI-IDEA" in content.body_text
    assert len(content.images) == 3
    assert all(
        str(item.url).startswith("https://uc.whu.edu.cn/__local/") for item in content.images
    )
    assert len(content.links) == 9
    assert str(content.links[0].url) == "https://diidea.pku.edu.cn/competition/"
    assert str(content.links[-1].url) == (
        "https://news.pku.edu.cn/xwzh/46fd7185501b4966991e1753d9b4d889.htm"
    )
    assert content.attachments == ()
    body = BeautifulSoup(content.body_html, "html.parser")
    assert len(body.select("p")) == 66
    assert len(body.select("img")) == 3
    assert not body.select("script, style, .title_nei, nav, footer")
    assert all(not image.has_attr("orisrc") for image in body.select("img"))
    assert "院内链接" not in content.body_text
    assert "作风建设" not in content.body_text
    assert FIXTURE.read_bytes() == original


@pytest.mark.parametrize(
    "mutate,field",
    [
        (lambda tree: tree.select_one("#vsb_content_501").decompose(), "body"),
        (lambda tree: tree.select_one("#vsb_content_501").__setitem__("id", "other"), "body"),
        (lambda tree: tree.select_one(".v_news_content").__setitem__("class", ["other"]), "body"),
        (
            lambda tree: tree.select_one("#vsb_content_501").append(
                BeautifulSoup('<div class="v_news_content">第二正文</div>', "html.parser")
            ),
            "body",
        ),
        (
            lambda tree: tree.select_one("#vsb_content_501").parent.append(
                BeautifulSoup('<div id="vsb_content"><div></div></div>', "html.parser")
            ),
            "body",
        ),
        (
            lambda tree: tree.select_one("#vsb_content_501").parent.append(
                BeautifulSoup('<div id="vsb_content_501"></div>', "html.parser")
            ),
            "body",
        ),
        (lambda tree: tree.select_one(".title_nei").decompose(), "header"),
        (
            lambda tree: tree.select_one(".title_nei").append(
                BeautifulSoup("<b>第二标题</b>", "html.parser")
            ),
            "title",
        ),
        (lambda tree: tree.select_one(".title_nei > i").decompose(), "published_date"),
    ],
)
def test_competition_template_missing_or_ambiguous_nodes_fail(mutate, field):
    with pytest.raises(ParseError) as caught:
        parse_notice(changed(mutate))
    assert caught.value.code == ParseErrorCode.MISSING_STRUCTURE
    assert caught.value.field == field
    assert len(str(caught.value)) < 150


def test_competition_template_requires_direct_body_and_valid_visible_date():
    def nest_body(tree):
        body = tree.select_one(".v_news_content")
        body.wrap(tree.new_tag("section"))

    with pytest.raises(ParseError, match="missing_structure field=body"):
        parse_notice(changed(nest_body))

    def invalid_date(tree):
        tree.select_one(".title_nei > i").string = "时间：2024-02-30"

    with pytest.raises(ParseError, match="invalid_field field=published_date"):
        parse_notice(changed(invalid_date))


@pytest.mark.parametrize("body", ["", "<script>有效竞赛</script>", "<p>&nbsp;</p>"])
def test_competition_template_still_rejects_meaningless_body(body):
    def replace_body(tree):
        node = tree.select_one(".v_news_content")
        node.clear()
        node.append(BeautifulSoup(body, "html.parser"))

    with pytest.raises(ParseError, match="meaningless_body field=body"):
        parse_notice(changed(replace_body))


def test_competition_template_normalization_is_deterministic_and_excludes_exterior():
    original = page()
    content = parse_notice(original).content
    assert parse_notice(original).content == content

    def exterior(tree):
        tree.select_one(".foot_title").string = "动态页脚改变 12345"
        node = tree.new_tag("span", id="nattach987")
        node.string = "99999"
        tree.select_one(".v_news_content").append(node)

    modified = changed(exterior)
    assert hashlib.sha256(modified.content).digest() != hashlib.sha256(original.content).digest()
    assert parse_notice(modified).content.content_sha256() == content.content_sha256()

    def meaningful(tree):
        tree.select_one(".v_news_content > p").append("参赛资格已变更。")

    assert parse_notice(changed(meaningful)).content.content_sha256() != content.content_sha256()


@pytest.mark.parametrize("maintenance_reparse", [False, True])
def test_real_v2_to_v3_same_raw_does_not_create_live_update(activated, maintenance_reparse):
    env = activated

    def previous_parser(page):
        return parse_notice(page).model_copy(update={"parser_version": "whu-student-notices-v2"})

    start_live(env, at=ACTIVATED_AT + 2, parser_version="whu-student-notices-v2")
    first = live(env, at=ACTIVATED_AT + 3, parser=previous_parser)
    assert len(rows(env, notification_events)) == 1
    if maintenance_reparse:
        reparse = process_response(env.engine, env.store, first.response_id, ACTIVATED_AT + 5)
        assert reparse.version_id != first.version_id
        assert observation(env)["version_id"] == first.version_id
        assert len(rows(env, notification_events)) == 1
    start_live(env, at=ACTIVATED_AT + 6)
    latest = live(env, at=ACTIVATED_AT + 7)
    assert latest.version_id != first.version_id
    assert observation(env)["version_id"] == latest.version_id
    assert observation(env)["comparison_error_code"] is None
    assert len(rows(env, notification_events)) == 1
    versions = rows(env, notice_versions)
    assert [version["parser_version"] for version in versions] == [
        "whu-student-notices-v2",
        "whu-student-notices-v3",
    ]
    assert versions[0]["content_sha256"] == versions[1]["content_sha256"]
    assert len(rows(env, raw_responses)) == 4
