"""Pure WHU undergraduate student-notice parsing, with no cache or I/O."""

import re
from datetime import date
from enum import StrEnum
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from pydantic import TypeAdapter

from signalnest.contracts import (
    AttachmentReference,
    ImageReference,
    LinkReference,
    ListEntry,
    ListPage,
    NoticeContent,
    PageInput,
    ParsedNotice,
    WebUrl,
)

PARSER_VERSION = "whu-student-notices-v1"
_WEB_URL = TypeAdapter(WebUrl)
_BLOCKS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "div",
        "dl",
        "dt",
        "dd",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }
)
_STAT_ID = re.compile(r"(?:nattach[0-9]+|dynclicks[0-9_]*|clickTimes)", re.IGNORECASE)


class ParseErrorCode(StrEnum):
    INVALID_UTF8 = "invalid_utf8"
    EMPTY_PAGE = "empty_page"
    MISSING_STRUCTURE = "missing_structure"
    INVALID_FIELD = "invalid_field"
    UNSUPPORTED_IDENTITY = "unsupported_identity"
    AMBIGUOUS_IDENTITY = "ambiguous_identity"
    EMPTY_LIST = "empty_list"
    MEANINGLESS_BODY = "meaningless_body"


class ParseError(ValueError):
    """Stable code and bounded context; never includes a URL, input value, or HTML."""

    def __init__(
        self, code: ParseErrorCode, *, field: str | None = None, item_index: int | None = None
    ):
        self.code = code
        self.field = field
        self.item_index = item_index  # Zero-based position; never a partially successful page.
        context = f" field={field}" if field else ""
        if item_index is not None:
            context += f" item_index={item_index}"
        super().__init__(f"{code.value}{context}")


def html_tree(page: PageInput) -> BeautifulSoup:
    """Build a tree using the observed source's UTF-8 encoding and explicit stdlib backend."""
    try:
        html = page.content.decode("utf-8")
    except UnicodeDecodeError:
        raise ParseError(ParseErrorCode.INVALID_UTF8) from None
    if not html.strip():
        raise ParseError(ParseErrorCode.EMPTY_PAGE)
    return BeautifulSoup(html, "html.parser")


def _text(node: Tag) -> str:
    # No separator between spans: Word-generated markup splits Chinese words and years.
    return " ".join(node.get_text().split())


def _one(root: Tag | BeautifulSoup, selector: str, field: str) -> Tag:
    nodes = root.select(selector)
    if len(nodes) != 1:
        raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field=field)
    return nodes[0]


def _required_text(node: Tag, field: str) -> str:
    text = _text(node)
    if not any(character.isalnum() for character in text):
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field)
    return text


def _date(text: str, field: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field) from None


def _reference(value: str, base: str, field: str) -> WebUrl | None:
    """Non-web links are omitted; malformed web links are explicit failures."""
    value = value.strip()
    if not value or value.startswith("#"):
        return None
    if any(ord(character) < 32 for character in value):
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field)
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"", "http", "https"}:
            return None
        if (parts.scheme or value.startswith("//")) and not parts.netloc:
            raise ValueError("web URL has no authority")
        return _WEB_URL.validate_python(urljoin(base, value))
    except ValueError:
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field) from None


def _required_url(value: str | None, base: str, field: str) -> WebUrl:
    url = _reference(value or "", base, field)
    if url is None:
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field)
    return url


def _query_value(query: dict[str, list[str]], key: str, field: str) -> str:
    values = query.get(key, [])
    if len(values) > 1:
        raise ParseError(ParseErrorCode.AMBIGUOUS_IDENTITY, field=field)
    if not values or not values[0]:
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field)
    return values[0]


def _identity(url: WebUrl) -> str:
    if url.host != "uc.whu.edu.cn" or url.port not in {80, 443}:
        raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity")
    parts = urlsplit(str(url))
    query = parse_qs(parts.query, keep_blank_values=True)
    match = re.fullmatch(r"/info/(1517)/([1-9][0-9]*)\.htm", parts.path)
    if match:
        category, article = match.groups()
        for key, expected in (("wbtreeid", category), ("wbnewsid", article)):
            if key in query and _query_value(query, key, "identity") != expected:
                raise ParseError(ParseErrorCode.AMBIGUOUS_IDENTITY, field="identity")
    elif parts.path == "/2022/show.jsp":
        category = _query_value(query, "wbtreeid", "identity")
        article = _query_value(query, "wbnewsid", "identity")
        if category != "1517" or not re.fullmatch(r"[1-9][0-9]*", article):
            raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity")
        if (
            "urltype" in query
            and _query_value(query, "urltype", "identity") != "news.NewsContentUrl"
        ):
            raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity")
    else:
        raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity")
    return f"{category}:{article}"


def _next_page(container: Tag, base: str) -> WebUrl | None:
    panels = container.parent.select(".page .p_pages")
    if not panels:
        return None
    if len(panels) != 1:
        raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="pagination")
    markers = panels[0].select(".p_next, .p_next_d")
    if not markers:
        if any(_text(a) == "下页" for a in panels[0].select("a")):
            raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="pagination")
        return None
    if len(markers) != 1:
        raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="pagination")
    marker = markers[0]
    anchors = marker.select("a")
    if "p_next_d" in marker.get("class", []) and not anchors:
        return None
    anchor = _one(marker, "a", "next_page_url")
    if _text(anchor) != "下页":
        raise ParseError(ParseErrorCode.INVALID_FIELD, field="next_page_url")
    url = _required_url(anchor.get("href"), base, "next_page_url")
    if url.host != "uc.whu.edu.cn" or not re.fullmatch(
        r"/tzgg/xstz(?:\.htm|/[1-9][0-9]*\.htm)", url.path or ""
    ):
        raise ParseError(ParseErrorCode.INVALID_FIELD, field="next_page_url")
    return url


def parse_list(page: PageInput) -> ListPage:
    tree = html_tree(page)
    container = _one(tree, "div.list_txt", "list")
    listing = _one(container, ":scope > ul.am-list", "entries")
    rows = listing.find_all(recursive=False)
    if not rows:
        raise ParseError(ParseErrorCode.EMPTY_LIST, field="entries")
    entries = []
    base = str(page.page_url)
    for index, row in enumerate(rows):
        try:
            if row.name != "li":
                raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="entry")
            anchor = _one(row, ":scope > a", "entry")
            url = _required_url(anchor.get("href"), base, "detail_url")
            entries.append(
                ListEntry(
                    source_document_id=_identity(url),
                    detail_url=url,
                    title=_required_text(_one(anchor, ":scope > span", "title"), "title"),
                    published_date=_date(
                        _text(_one(anchor, ":scope > i", "published_date")), "published_date"
                    ),
                )
            )
        except ParseError as exc:
            raise ParseError(exc.code, field=exc.field, item_index=index) from None
    return ListPage(entries=tuple(entries), next_page_url=_next_page(container, base))


def _attachments(region: Tag, base: str) -> tuple[AttachmentReference, ...]:
    panels = region.select("div.fj")
    if not panels:
        return ()
    if len(panels) != 1:
        raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="attachments")
    listing = _one(panels[0], ":scope > ul", "attachments")
    attachments = []
    for index, row in enumerate(listing.find_all(recursive=False)):
        try:
            if row.name != "li":
                raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="attachment")
            anchor = _one(row, "a", "attachment")
            url = _required_url(anchor.get("href"), base, "attachment_url")
            identifier = None
            if url.host == "uc.whu.edu.cn" and url.path == "/system/_content/download.jsp":
                query = parse_qs(urlsplit(str(url)).query, keep_blank_values=True)
                owner = _query_value(query, "owner", "attachment_identity")
                file_id = _query_value(query, "wbfileid", "attachment_identity")
                if not re.fullmatch(r"[0-9]+", owner) or not re.fullmatch(r"[A-Za-z0-9]+", file_id):
                    raise ParseError(ParseErrorCode.INVALID_FIELD, field="attachment_identity")
                identifier = f"{owner}:{file_id}"
            attachments.append(
                AttachmentReference(
                    name=_required_text(anchor, "attachment_name"),
                    url=url,
                    source_attachment_id=identifier,
                )
            )
        except ParseError as exc:
            raise ParseError(exc.code, field=exc.field, item_index=index) from None
    return tuple(attachments)


def _hidden(node: Tag) -> bool:
    style = re.sub(r"\s+", "", node.get("style", "")).lower()
    return (
        node.has_attr("hidden")
        or node.get("aria-hidden") == "true"
        or bool(
            re.search(r"(?:^|;)(?:display:none|visibility:hidden)(?:!important)?(?:;|$)", style)
        )
    )


def _clean_body(body: Tag) -> None:
    if _hidden(body):
        body.clear()
        return
    for node in list(body.find_all(True)):
        if node.parent is None:
            continue  # It was inside an already removed component.
        if (
            node.name in {"script", "style", "noscript", "template", "nav", "footer", "form"}
            or _hidden(node)
            or _STAT_ID.fullmatch(node.get("id", ""))
            or {"fj", "title_nei", "p_pages"}.intersection(node.get("class", []))
        ):
            node.decompose()
            continue
        for attr in list(node.attrs):
            if attr.lower().startswith("on") or attr in {
                "vurl",
                "orisrc",
                "vsbhref",
                "vheight",
                "vwidth",
                "srcset",  # Only the explicit src fallback is supported by the image contract.
            }:
                del node[attr]
    for node in list(body.find_all(string=True)):
        if isinstance(node, Comment):
            node.extract()
        elif not node.find_parent(["pre", "code"]):
            text = " ".join(str(node).split())
            if text:
                # Retain boundary spaces between inline runs; collapse formatting whitespace.
                original = str(node)
                text = (" " if original[0].isspace() else "") + text
                text += " " if original[-1].isspace() else ""
                if node.parent.name in _BLOCKS:
                    if node.previous_sibling is None:
                        text = text.lstrip()
                    if node.next_sibling is None:
                        text = text.rstrip()
                node.replace_with(text)
            elif (
                node.previous_sibling is not None
                and node.next_sibling is not None
                and getattr(node.previous_sibling, "name", None) not in _BLOCKS
                and getattr(node.next_sibling, "name", None) not in _BLOCKS
            ):
                node.replace_with(" ")
            else:
                node.extract()


def _body_text(body: Tag) -> str:
    parts = []

    def visit(node: Tag | NavigableString) -> None:
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if node.name in _BLOCKS or node.name == "br":
            parts.append("\n")
        for child in node.children:
            visit(child)
        if node.name in {"td", "th"}:
            parts.append("\t")
        elif node.name in _BLOCKS:
            parts.append("\n")

    visit(body)
    return "\n".join(line.strip() for line in "".join(parts).splitlines() if line.strip())


def parse_notice(page: PageInput) -> ParsedNotice:
    identity = _identity(page.page_url)
    tree = html_tree(page)
    # Both fixtures contain a nested .news_show for surrounding page components.
    region = _one(tree, "div.news_show:not(.news_show .news_show)", "notice")
    header = _one(region, ":scope > .title_nei", "header")
    title = _required_text(_one(header, ":scope > b", "title"), "title")
    date_text = _text(_one(header, ":scope > i", "published_date"))
    match = re.fullmatch(r"时间\s*[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", date_text)
    if match is None:
        raise ParseError(ParseErrorCode.INVALID_FIELD, field="published_date")
    published = _date(match[1], "published_date")
    body = _one(region, "#vsb_content > .v_news_content", "body")
    base = str(page.page_url)
    attachments = _attachments(region, base)
    _clean_body(body)
    links = []
    for anchor in body.select("a[href]"):
        url = _reference(anchor["href"], base, "body_link")
        if url is None:
            del anchor[
                "href"
            ]  # Keep visible text, never reinterpret mailto/js/anchors as web URLs.
        else:
            anchor["href"] = str(url)
            links.append(LinkReference(url=url, text=_text(anchor)))
    images = []
    for image in body.select("img"):
        if image.get("width") == "1" and image.get("height") == "1":
            image.decompose()  # Explicit 1x1 tracking/decorative image, not a timetable.
            continue
        url = _required_url(image.get("src"), base, "image_url")
        image["src"] = str(url)
        images.append(ImageReference(url=url, alt_text=image.get("alt", "")))
    text = _body_text(body)
    if not any(character.isalnum() for character in text) and not images:
        raise ParseError(ParseErrorCode.MEANINGLESS_BODY, field="body")
    content = NoticeContent(
        title=title,
        published_date=published,
        body_html=body.decode_contents(),
        body_text=text,
        links=tuple(links),
        images=tuple(images),
        attachments=attachments,
    )
    return ParsedNotice(
        source_document_id=identity,
        page_url=page.page_url,
        parser_version=PARSER_VERSION,
        content=content,
    )
