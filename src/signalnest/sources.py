"""Two explicit source bindings, with finite HTTP routes and lazy pure Parsers.

Configuration selects a binding; a host, document ID or failed parse never selects
another one. This is not a dynamic registry or a general fetching framework.
"""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import TypeAdapter, ValidationError

from signalnest.contracts import WebUrl

SourceName = Literal["whu-student-notices", "cs-undergrad-notices"]
_URL = TypeAdapter(WebUrl)


@dataclass(frozen=True)
class SourceBinding:
    name: SourceName
    source_id: str
    host: str
    list_path: str
    column: str
    legacy_path: str

    def parsers(self):
        # Configuration/help do not load Parsers, and UC does not import CS.
        if self.name == "whu-student-notices":
            from signalnest.parsing import PARSER_VERSION, parse_list, parse_notice

            return parse_list, parse_notice, PARSER_VERSION
        from signalnest.cs_parsing import PARSER_VERSION, parse_cs_list, parse_cs_notice

        return parse_cs_list, parse_cs_notice, PARSER_VERSION

    def target_uri(self, value: str, page_type: str, identity: str | None = None) -> str:
        """Validate every request/redirect, retaining the actual ordered query.

        JSP is an allowed same-source identity route, not permission to follow login
        or third-party redirects. A 200 still needs the bound Parser to validate it.
        """
        if any(ord(c) <= 32 or ord(c) == 127 for c in value) or any(c in value for c in "\\#"):
            raise ValueError("invalid_target")
        try:
            url = _URL.validate_python(value)
            uri = str(url)
            parts = urlsplit(uri)
            if url.scheme != "https" or url.host != self.host or url.port != 443:
                raise ValueError("invalid_target")
            if page_type == "list":
                if "?" in value or not re.fullmatch(
                    re.escape(self.list_path) + r"(?:\.htm|/[1-9][0-9]*\.htm)", parts.path
                ):
                    raise ValueError("invalid_target")
            elif page_type == "notice":
                query = parse_qs(parts.query, keep_blank_values=True)
                match = re.fullmatch(rf"/info/({self.column})/([1-9][0-9]*)\.htm", parts.path)
                if match:
                    category, article = match.groups()
                    if any(
                        key in query and query[key] != [expected]
                        for key, expected in (("wbtreeid", category), ("wbnewsid", article))
                    ):
                        raise ValueError("invalid_target")
                elif parts.path == self.legacy_path:
                    if len(query.get("wbtreeid", [])) != 1 or len(query.get("wbnewsid", [])) != 1:
                        raise ValueError("invalid_target")
                    category, article = query["wbtreeid"][0], query["wbnewsid"][0]
                    if category != self.column or not re.fullmatch(r"[1-9][0-9]*", article):
                        raise ValueError("invalid_target")
                else:
                    raise ValueError("invalid_target")
                if "urltype" in query and query["urltype"] != ["news.NewsContentUrl"]:
                    raise ValueError("invalid_target")
                if f"{category}:{article}" != identity:
                    raise ValueError("invalid_target")
            else:
                raise ValueError("invalid_target")
            return str(httpx.URL(uri))
        except (ValidationError, httpx.InvalidURL) as exc:
            raise ValueError("invalid_target") from exc


UC = SourceBinding(
    "whu-student-notices",
    "whu-undergrad-student",
    "uc.whu.edu.cn",
    "/tzgg/xstz",
    "1517",
    "/2022/show.jsp",
)
CS = SourceBinding(
    "cs-undergrad-notices",
    "whu-cs-undergrad-teaching",
    "cs.whu.edu.cn",
    "/xwdt/tzgg/bkjx",
    "1074",
    "/content.jsp",
)


def source_binding(name: SourceName) -> SourceBinding:
    if name == UC.name:
        return UC
    if name == CS.name:
        return CS
    raise ValueError("unsupported_source_parser")
