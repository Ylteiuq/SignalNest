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
    PaginationEvidence,
    ParsedNotice,
    WebUrl,
)

PARSER_VERSION = "whu-student-notices-v3"
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
    INVALID_PAGINATION = "invalid_pagination"


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


def _list_url(value: str | None, base: str, field: str) -> WebUrl:
    # urljoin can erase an empty query/fragment; reject unsupported syntax first.
    if value is not None and any(character in value for character in "\\?#"):
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field)
    url = _required_url(value, base, field)
    if (
        url.scheme != "https"
        or url.host != "uc.whu.edu.cn"
        or url.port != 443
        or url.query is not None
        or url.fragment is not None
        or not re.fullmatch(r"/tzgg/xstz(?:\.htm|/[1-9][0-9]*\.htm)", url.path or "")
    ):
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field)
    return url


def _page_number(node: Tag, field: str) -> int:
    value = _text(node)
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field)
    try:
        return int(value)
    except ValueError:  # Includes excessively long, untrusted integer strings.
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field) from None


def _page_control(panel: Tag, kind: str, label: str, base: str) -> WebUrl | None:
    field = f"{kind}_page_url"
    active, disabled = f"p_{kind}", f"p_{kind}_d"
    marker = _one(panel, f".{active}, .{disabled}", field)
    classes = marker.get("class", [])
    is_disabled = disabled in classes
    if (
        marker.name != "span"
        or marker.parent is not panel
        or (active in classes) == is_disabled
        or ("p_fun" in classes and is_disabled)
        or ("p_fun_d" in classes and not is_disabled)
        or _text(marker) != label
    ):
        raise ParseError(ParseErrorCode.INVALID_PAGINATION, field=field)
    if is_disabled:
        if marker.select("a"):
            raise ParseError(ParseErrorCode.INVALID_PAGINATION, field=field)
        return None
    anchor = _one(marker, ":scope > a", field)
    if len(marker.select("a")) != 1:
        raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field=field)
    return _list_url(anchor.get("href"), base, field)


def _pagination(container: Tag, base: str) -> tuple[PaginationEvidence, WebUrl | None]:
    # Require the observed sibling region; no paginator is never evidence of termination.
    region = _one(container.parent, ".page", "pagination")
    panel = _one(region, ".p_pages", "pagination")
    if region.parent is not container.parent or panel.parent is not region:
        raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="pagination")
    # Explicitly hidden markers cannot support a visible page declaration.
    evidence_nodes = [region, panel, *region.parents]
    for marker in panel.select(".p_no, .p_no_d, .p_dot, .p_next, .p_next_d, .p_last, .p_last_d"):
        evidence_nodes.extend((marker, *marker.find_all(True)))
    if any(_hidden(node) for node in evidence_nodes):
        raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="pagination")
    current = _one(panel, ".p_no_d", "current_page")
    current_page = _page_number(current, "current_page")
    page_url = _list_url(base, base, "page_url")
    numbers: dict[int, WebUrl | None] = {}
    previous = 0
    for node in panel.select(".p_no, .p_no_d"):
        classes = node.get("class", [])
        if (
            node.name != "span"
            or node.parent is not panel
            or ("p_no" in classes and "p_no_d" in classes)
        ):
            raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="page_numbers")
        number = _page_number(node, "current_page" if node is current else "page_numbers")
        if number <= previous:
            raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="page_numbers")
        if previous and number > previous + 1:
            # Visible gaps need the template's explicit ellipsis, not a missing page label.
            sibling = node.find_previous_sibling()
            if (
                sibling is None
                or "p_dot" not in sibling.get("class", [])
                or _text(sibling) != "..."
            ):
                raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="page_numbers")
        if node is current:
            if node.select("a"):
                raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="current_page")
            numbers[number] = None
        else:
            anchor = _one(node, ":scope > a", "page_numbers")
            if len(node.select("a")) != 1:
                raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="page_numbers")
            numbers[number] = _list_url(anchor.get("href"), base, "page_numbers")
        previous = number
    if next(iter(numbers)) != 1:
        raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="page_numbers")
    # A URL claiming two different visible page numbers is contradictory evidence.
    targets = [url for url in numbers.values() if url is not None]
    if len(set(targets)) != len(targets) or page_url in targets:
        raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="page_numbers")
    total_pages = max(numbers)
    next_url = _page_control(panel, "next", "下页", base)
    last_url = _page_control(panel, "last", "尾页", base)
    is_last = current_page == total_pages
    if is_last:
        if next_url is not None or last_url is not None:
            raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="pagination")
    else:
        if next_url is None or last_url is None or last_url != numbers[total_pages]:
            raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="pagination")
        if next_url == page_url:
            raise ParseError(ParseErrorCode.INVALID_FIELD, field="next_page_url")
        if (current_page + 1 in numbers and next_url != numbers[current_page + 1]) or any(
            url == next_url and number != current_page + 1 for number, url in numbers.items()
        ):
            raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="next_page_url")
    return PaginationEvidence(
        current_page=current_page,
        total_pages=total_pages,
        is_last_page=is_last,
        terminal_evidence="disabled_next_and_last" if is_last else None,
        last_page_url=last_url,
    ), next_url


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
    pagination, next_url = _pagination(container, base)
    return ListPage(entries=tuple(entries), next_page_url=next_url, pagination=pagination)


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
    # Only the two observed source templates are supported. A second wrapper,
    # even an empty one, is ambiguous; never fall back to an arbitrary content div.
    wrapper = _one(region, "div#vsb_content, div#vsb_content_501", "body")
    body = _one(wrapper, ":scope > .v_news_content", "body")
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
