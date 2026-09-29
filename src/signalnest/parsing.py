"""Pure HTML preparation, not the site's list/detail parser.

Next-stage functions: parse_list(page: PageInput) -> ListPage and
parse_notice(page: PageInput) -> ParsedNotice. Neither may access network/storage.
"""

from bs4 import BeautifulSoup

from signalnest.contracts import PageInput


class ParseError(ValueError):
    """Parsing failed; callers must not record this as an empty successful result."""


def html_tree(page: PageInput) -> BeautifulSoup:
    """Build a tree using the observed source's UTF-8 encoding and explicit stdlib backend."""
    try:
        html = page.content.decode("utf-8")
    except UnicodeDecodeError:
        raise ParseError("invalid_utf8") from None
    if not html.strip():
        raise ParseError("empty_page")
    return BeautifulSoup(html, "html.parser")
