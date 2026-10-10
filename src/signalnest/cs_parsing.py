"""Pure offline parsing of the observed CS undergraduate-teaching list/detail.

This adapter does not enable a collection source. Routes are validated independently
of UC/EMS; raw captures, decision clocks and future fetch permissions stay explicit.
"""

import re
from urllib.parse import parse_qs, urlsplit

from bs4 import Tag

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
    PendingReference,
    WebUrl,
)
from signalnest.parsing import (
    ParseError,
    ParseErrorCode,
    _body_text,
    _clean_body,
    _date,
    _hidden,
    _one,
    _page_number,
    _query_value,
    _reference,
    _required_text,
    _required_url,
    _text,
    html_tree,
)

PARSER_VERSION = "cs-undergrad-notices-v1"
SOURCE_ID = "whu-cs-undergrad-teaching"
LIST_URL = "https://cs.whu.edu.cn/xwdt/tzgg/bkjx.htm"


def _article(url: WebUrl) -> tuple[str, str]:
    """Validate observed article routes, including redundant query identities."""
    parts = urlsplit(str(url))
    query = parse_qs(parts.query, keep_blank_values=True)
    match = re.fullmatch(r"/info/([1-9][0-9]*)/([1-9][0-9]*)\.htm", parts.path)
    if match:
        column, article = match.groups()
        for key, expected in (("wbtreeid", column), ("wbnewsid", article)):
            if key in query and _query_value(query, key, "identity") != expected:
                raise ParseError(ParseErrorCode.AMBIGUOUS_IDENTITY, field="identity")
    elif parts.path == "/content.jsp":
        column = _query_value(query, "wbtreeid", "identity")
        article = _query_value(query, "wbnewsid", "identity")
        if not all(re.fullmatch(r"[1-9][0-9]*", item) for item in (column, article)):
            raise ParseError(ParseErrorCode.INVALID_FIELD, field="identity")
    else:
        raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity")
    if "urltype" in query and _query_value(query, "urltype", "identity") != "news.NewsContentUrl":
        raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity")
    return column, article


def _identity(url: WebUrl) -> str:
    if (
        url.scheme != "https"
        or url.host != "cs.whu.edu.cn"
        or url.port != 443
        or url.fragment is not None
    ):
        raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity")
    column, article = _article(url)
    if column != "1074":
        raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity")
    return f"{column}:{article}"


def _list_target(url: WebUrl) -> str | None:
    if url.host != "cs.whu.edu.cn" or url.scheme != "https" or url.port != 443:
        return "external"
    path = url.path or ""
    if not (path.startswith("/info/") or path == "/content.jsp"):
        return "unsupported_route"
    # A damaged known article is a failed page, never an unadapted-reference shortcut.
    column, _ = _article(url)
    if column != "1074":
        return "unsupported_column"
    return "unsupported_route" if url.fragment is not None else None


def _list_url(value: str | None, base: str, field: str) -> WebUrl:
    if value is not None and any(character in value for character in "\\?#"):
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field)
    url = _required_url(value, base, field)
    if (
        url.scheme != "https"
        or url.host != "cs.whu.edu.cn"
        or url.port != 443
        or url.query is not None
        or url.fragment is not None
        or not re.fullmatch(r"/xwdt/tzgg/bkjx(?:\.htm|/[1-9][0-9]*\.htm)", url.path or "")
    ):
        raise ParseError(ParseErrorCode.INVALID_FIELD, field=field)
    return url


def _control(panel: Tag, kind: str, label: str, base: str) -> WebUrl | None:
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
    region = _one(container, ":scope > .pagebar", "pagination")
    panel = _one(region, ":scope > .p_pages", "pagination")
    if len(container.select(".pagebar")) != 1 or len(region.select(".p_pages")) != 1:
        raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="pagination")
    if any(_hidden(node) for node in (*region.parents, region, panel, *panel.find_all(True))):
        raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="pagination")
    current = _one(panel, ".p_no_d", "current_page")
    current_page = _page_number(current, "current_page")
    page_url = _list_url(base, base, "page_url")
    numbers: dict[int, WebUrl | None] = {}
    previous = 0
    for node in panel.select(".p_no, .p_no_d"):
        classes = node.get("class", [])
        number = _page_number(node, "current_page" if node is current else "page_numbers")
        if (
            node.name != "span"
            or node.parent is not panel
            or ("p_no" in classes and "p_no_d" in classes)
            or number <= previous
        ):
            raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="page_numbers")
        if previous and number != previous + 1:
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
    targets = [url for url in numbers.values() if url is not None]
    if next(iter(numbers)) != 1 or len(set(targets)) != len(targets) or page_url in targets:
        raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="page_numbers")
    total = max(numbers)
    first_url = _control(panel, "first", "首页", base)
    prev_url = _control(panel, "prev", "上一页", base)
    next_url = _control(panel, "next", "下一页", base)
    last_url = _control(panel, "last", "尾页", base)
    if current_page == 1:
        if first_url is not None or prev_url is not None:
            raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="pagination")
    elif (
        first_url != numbers[1]
        or prev_url is None
        or prev_url == page_url
        or (current_page - 1 in numbers and prev_url != numbers[current_page - 1])
        or any(url == prev_url and number != current_page - 1 for number, url in numbers.items())
    ):
        raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="pagination")
    is_last = current_page == total
    if is_last:
        if next_url is not None or last_url is not None:
            raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="pagination")
    elif (
        next_url is None
        or last_url is None
        or last_url != numbers[total]
        or next_url == page_url
        or (current_page + 1 in numbers and next_url != numbers[current_page + 1])
        or any(url == next_url and number != current_page + 1 for number, url in numbers.items())
    ):
        raise ParseError(ParseErrorCode.INVALID_PAGINATION, field="pagination")
    return PaginationEvidence(
        current_page=current_page,
        total_pages=total,
        is_last_page=is_last,
        terminal_evidence="disabled_next_and_last" if is_last else None,
        last_page_url=last_url,
    ), next_url


def parse_cs_list(page: PageInput) -> ListPage:
    """Validate every observed row and paginator; do not infer a complete scan."""
    base = str(page.page_url)
    _list_url(base, base, "page_url")
    tree = html_tree(page)
    container = _one(tree, "div.study.under-new", "list")
    listing = _one(container, ":scope > ul", "entries")
    rows = listing.find_all(recursive=False)
    if not rows:
        raise ParseError(ParseErrorCode.EMPTY_LIST, field="entries")
    entries, references = [], []
    for index, row in enumerate(rows):
        try:
            if row.name != "li":
                raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="entry")
            anchor = _one(row, ":scope > a", "entry")
            if len(row.select("a")) != 1:
                raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="entry")
            raw_href = anchor.get("href")
            if (
                not isinstance(raw_href, str)
                or len(raw_href) > 8192
                or any(
                    ord(character) <= 32 or ord(character) == 127 or character == "\\"
                    for character in raw_href
                )
                or re.search(r"%(?![0-9A-Fa-f]{2})", raw_href)
            ):
                raise ParseError(ParseErrorCode.INVALID_FIELD, field="detail_url")
            url = _required_url(raw_href, base, "detail_url")
            title = _required_text(_one(anchor, ":scope > p", "title"), "title")
            published = _date(
                _text(_one(anchor, ":scope > span", "published_date")), "published_date"
            )
            kind = _list_target(url)
            if kind is None:
                entries.append(
                    ListEntry(
                        source_document_id=_identity(url),
                        detail_url=url,
                        title=title,
                        published_date=published,
                    )
                )
            else:
                references.append(
                    PendingReference(
                        raw_href=raw_href,
                        resolved_url=url,
                        title=title,
                        published_date=published,
                        row_index=index,
                        reference_kind=kind,
                    )
                )
        except ParseError as exc:
            raise ParseError(exc.code, field=exc.field, item_index=index) from None
    pagination, next_url = _pagination(container, base)
    return ListPage(
        entries=tuple(entries),
        references=tuple(references),
        next_page_url=next_url,
        pagination=pagination,
    )


def parse_cs_notice(page: PageInput) -> ParsedNotice:
    """Parse the recorded CS article template; no template/route guessing on failure."""
    identity = _identity(page.page_url)
    tree = html_tree(page)
    region = _one(tree, "div.article", "notice")
    article = _one(region, ':scope > form[name="_newscontent_fromname"]', "notice")
    title_node = _one(article, ":scope > h2.title", "title")
    date_node = _one(article, ":scope > h4.information", "published_date")
    for field, node in (("title", title_node), ("published_date", date_node)):
        if any(_hidden(item) for item in (node, *node.parents, *node.find_all(True))):
            raise ParseError(ParseErrorCode.INVALID_FIELD, field=field)
    title = _required_text(title_node, "title")
    date_text = _text(date_node)
    match = re.fullmatch(
        r"发布时间\s*[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s*浏览量\s*[:：]\s*[0-9]*次",
        date_text,
    )
    if match is None:
        raise ParseError(ParseErrorCode.INVALID_FIELD, field="published_date")
    published = _date(match[1], "published_date")
    content_region = _one(article, ":scope > div.content", "body")
    wrapper = _one(content_region, ":scope > div#vsb_content", "body")
    body = _one(wrapper, ":scope > .v_news_content", "body")
    _clean_body(body)
    links, images, attachments = [], [], []
    base = str(page.page_url)
    for anchor in body.select("a[href]"):
        url = _reference(anchor["href"], base, "body_link")
        if url is None:
            del anchor["href"]
            continue
        anchor["href"] = str(url)
        links.append(LinkReference(url=url, text=_text(anchor)))
        if url.host == "cs.whu.edu.cn" and url.path == "/system/_content/download.jsp":
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
            attachments=tuple(attachments),
        ),
    )
