from pathlib import Path

import httpx
import pytest

from signalnest.config import load_config
from signalnest.contracts import PageInput
from signalnest.fetching import make_client
from signalnest.parsing import ParseError, html_tree

ROOT = Path(__file__).resolve().parents[1]


def test_client_configuration_and_no_implicit_requests(monkeypatch):
    settings = load_config(ROOT / "config.example.toml").http
    real_client = httpx.Client
    requests = []
    options = {}
    transport_options = {}

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example.org/"})

    def client_with_mock_transport(**kwargs):
        options.update(kwargs)
        return real_client(**kwargs)

    def mock_transport(**kwargs):
        transport_options.update(kwargs)
        return httpx.MockTransport(respond)

    monkeypatch.setattr(httpx, "Client", client_with_mock_transport)
    monkeypatch.setattr(httpx, "HTTPTransport", mock_transport)
    with make_client(settings) as client:
        assert requests == []
        assert client.timeout.connect == settings.connect_timeout_seconds
        assert client.timeout.read == settings.read_timeout_seconds
        assert client.timeout.write > 0
        assert client.timeout.pool > 0
        response = client.get("https://example.org/notice")
        assert response.status_code == 302
        assert len(requests) == 1
        assert requests[0].headers["User-Agent"] == "SignalNest/0.1"
        assert requests[0].headers["Accept"] == "text/html"
        assert requests[0].headers["Accept-Encoding"] == "identity"
    assert client.is_closed
    assert options["verify"] is True
    assert options["trust_env"] is False
    assert options["limits"].max_connections == 1
    assert transport_options["retries"] == 0
    assert transport_options["verify"] is True
    assert transport_options["trust_env"] is False


def test_explicit_parser_backend_with_existing_fixture():
    raw = (ROOT / "research/fixtures/student-notices-page1.html").read_bytes()
    page = PageInput(content=raw, page_url="https://uc.whu.edu.cn/tzgg/xstz.htm")
    tree = html_tree(page)
    assert tree.builder.NAME == "html.parser"
    assert "学生通知" in tree.title.get_text()
    assert page.content == raw


@pytest.mark.parametrize(("raw", "error"), [(b" \n\t", "empty_page"), (b"\xff", "invalid_utf8")])
def test_html_preparation_fails_explicitly(raw, error):
    with pytest.raises(ParseError, match=error):
        html_tree(PageInput(content=raw, page_url="https://example.org/"))
