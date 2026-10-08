"""Pure parsing of the observed EMS notice-detail template, for offline replay.

This is not a production source adapter: no list parser, HTTP access, authentication,
or attachment downloads are provided. Shared WHU helpers keep body normalization and
HTTP(S) reference semantics aligned without broadening the undergraduate parser.
"""

import re
from urllib.parse import parse_qs, urlsplit

from bs4 import Tag

from signalnest.contracts import (
    AttachmentReference,
    ImageReference,
    LinkReference,
    NoticeContent,
    PageInput,
    ParsedNotice,
    WebUrl,
)
from signalnest.parsing import (
    ParseError,
    ParseErrorCode,
    _body_text,
    _clean_body,
    _date,
    _one,
    _query_value,
    _reference,
    _required_text,
    _required_url,
    _text,
    html_tree,
)

PARSER_VERSION = "ems-notices-v1"


def _identity(url: WebUrl) -> str:
    if url.host != "ems.whu.edu.cn" or url.port not in {80, 443} or url.fragment is not None:
        raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity")
    parts = urlsplit(str(url))
    query = parse_qs(parts.query, keep_blank_values=True)
    match = re.fullmatch(r"/info/(1588)/([1-9][0-9]*)\.htm", parts.path)
    if match:
        category, article = match.groups()
        for key, expected in (("wbtreeid", category), ("wbnewsid", article)):
            if key in query and _query_value(query, key, "identity") != expected:
                raise ParseError(ParseErrorCode.AMBIGUOUS_IDENTITY, field="identity")
    elif parts.path == "/2025/content.jsp":
        category = _query_value(query, "wbtreeid", "identity")
        article = _query_value(query, "wbnewsid", "identity")
        if category != "1588" or not re.fullmatch(r"[1-9][0-9]*", article):
            raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity")
    else:
        raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity")
    if "urltype" in query and _query_value(query, "urltype", "identity") != "news.NewsContentUrl":
        raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity")
    return f"{category}:{article}"


def _attachments(wrapper: Tag, base: str) -> tuple[AttachmentReference, ...]:
    # Observed attachment paragraphs sit outside .v_news_content, beside prev/next links.
    # Only this explicit shape is accepted; their counters never become normalized text.
    attachments = []
    for index, row in enumerate(wrapper.select(":scope > p:not(.pre):not(.next)")):
        try:
            anchor = _one(row, ":scope > a", "attachment")
            if len(row.select("a")) != 1:
                raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="attachment")
            url = _required_url(anchor.get("href"), base, "attachment_url")
            if url.host != "ems.whu.edu.cn" or url.path != "/system/_content/download.jsp":
                raise ParseError(ParseErrorCode.INVALID_FIELD, field="attachment_url")
            query = parse_qs(urlsplit(str(url)).query, keep_blank_values=True)
            owner = _query_value(query, "owner", "attachment_identity")
            file_id = _query_value(query, "wbfileid", "attachment_identity")
            if not re.fullmatch(r"[0-9]+", owner) or not re.fullmatch(r"[A-Za-z0-9]+", file_id):
                raise ParseError(ParseErrorCode.INVALID_FIELD, field="attachment_identity")
            if (
                "urltype" in query
                and _query_value(query, "urltype", "attachment_identity")
                != "news.DownloadAttachUrl"
            ):
                raise ParseError(ParseErrorCode.INVALID_FIELD, field="attachment_identity")
            attachments.append(
                AttachmentReference(
                    name=_required_text(anchor, "attachment_name"),
                    url=url,
                    source_attachment_id=f"{owner}:{file_id}",
                )
            )
        except ParseError as exc:
            raise ParseError(exc.code, field=exc.field, item_index=index) from None
    return tuple(attachments)


def parse_ems_notice(page: PageInput) -> ParsedNotice:
    """Parse one supported EMS 1588 detail; all bytes are validated on every call.

    A login redirect or a news-domain page is not a supported article. This function
    neither discovers EMS lists nor makes the EMS site an enabled collection source.
    """
    identity = _identity(page.page_url)
    tree = html_tree(page)
    region = _one(tree, "div.article", "notice")
    article = _one(region, ':scope > form[name="_newscontent_fromname"]', "notice")
    title = _required_text(_one(article, ":scope > h2#titleStr", "title"), "title")
    date_text = _text(_one(article, ":scope > h4.timeandhit", "published_date"))
    match = re.fullmatch(
        r"发布时间\s*[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s*阅读\s*[:：]\s*[0-9]*",
        date_text,
    )
    if match is None:
        raise ParseError(ParseErrorCode.INVALID_FIELD, field="published_date")
    published = _date(match[1], "published_date")
    wrapper = _one(article, ":scope > div#vsb_content_2", "body")
    body = _one(wrapper, ":scope > .v_news_content", "body")
    base = str(page.page_url)
    attachments = _attachments(wrapper, base)
    _clean_body(body)
    links = []
    for anchor in body.select("a[href]"):
        url = _reference(anchor["href"], base, "body_link")
        if url is None:
            del anchor["href"]
        else:
            anchor["href"] = str(url)
            links.append(LinkReference(url=url, text=_text(anchor)))
    images = []
    for image in body.select("img"):
        if image.get("width") == "1" and image.get("height") == "1":
            image.decompose()
            continue
        url = _required_url(image.get("src"), base, "image_url")
        image["src"] = str(url)
        images.append(ImageReference(url=url, alt_text=image.get("alt", "")))
    text = _body_text(body)
    if not any(character.isalnum() for character in text) and not images:
        raise ParseError(ParseErrorCode.MEANINGLESS_BODY, field="body")
    return ParsedNotice(
        source_document_id=identity,
        page_url=page.page_url,
        parser_version=PARSER_VERSION,
        content=NoticeContent(
            title=title,
            published_date=published,
            body_html=body.decode_contents(),
            body_text=text,
            links=tuple(links),
            images=tuple(images),
            attachments=attachments,
        ),
    )
