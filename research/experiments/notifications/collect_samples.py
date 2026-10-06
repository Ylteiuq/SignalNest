"""Finite research capture; no retries, pagination, attachments or production writes.

Run from repository root: .venv/bin/python research/experiments/notifications/collect_samples.py
"""

from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[3]
DEST = ROOT / "research" / "fixtures" / "notifications"
URLS = (
    "https://uc.whu.edu.cn/info/1517/128291.htm",
    "https://uc.whu.edu.cn/info/1517/17361.htm",
    "https://uc.whu.edu.cn/info/1517/14147.htm",
    "https://uc.whu.edu.cn/info/1517/117011.htm",
    "https://uc.whu.edu.cn/info/1517/18135.htm",
    "https://uc.whu.edu.cn/2022/show.jsp?urltype=news.NewsContentUrl&wbtreeid=1517&wbnewsid=127511",
)
SAFE_HEADERS = (
    "date", "content-type", "content-length", "etag", "last-modified",
    "cache-control", "location", "server",
)


def identifier(url):
    parsed = urlsplit(url)
    return parse_qs(parsed.query)["wbnewsid"][0] if parsed.query else parsed.path.rsplit("/", 1)[-1][:-4]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="+", choices=[identifier(url) for url in URLS])
    selected = parser.parse_args().ids
    DEST.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        trust_env=False, verify=True, follow_redirects=False,
        headers={"User-Agent": "SignalNest/0.1 notification-rule-research", "Accept": "text/html"},
        timeout=httpx.Timeout(20.0, connect=10.0),
    ) as client:
        urls = [url for url in URLS if selected is None or identifier(url) in selected]
        for index, url in enumerate(urls):
            if index:
                time.sleep(5)
            started = datetime.now(timezone.utc)
            stem = f"notice-{identifier(url)}-{started.strftime('%Y%m%dT%H%M%S%fZ')}"
            meta = {"requested_url": url, "started_at": started.isoformat(), "request_method": "GET"}
            try:
                chunks = []
                size = 0
                with client.stream("GET", url) as response:
                    meta.update(status_code=response.status_code, final_url=str(response.url))
                    meta["headers"] = {key: response.headers[key] for key in SAFE_HEADERS if key in response.headers}
                    if "set-cookie" in response.headers:
                        meta["headers"]["set-cookie"] = "[REDACTED]"
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 1_048_576:
                            raise ValueError("research_body_limit")
                        chunks.append(chunk)
                body = b"".join(chunks)
                meta.update(finished_at=datetime.now(timezone.utc).isoformat(), body_bytes=len(body), sha256=sha256(body).hexdigest())
                meta["fixture"] = f"{stem}.html"
                (DEST / meta["fixture"]).write_bytes(body)
                soup = BeautifulSoup(body, "html.parser")
                title = soup.select_one(".title_nei b")
                meta["title"] = title.get_text(" ", strip=True) if title else None
            except (httpx.HTTPError, OSError, ValueError) as exc:
                meta.update(finished_at=datetime.now(timezone.utc).isoformat(), error_type=type(exc).__name__)
            (DEST / f"{stem}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({key: meta.get(key) for key in ("requested_url", "status_code", "body_bytes", "title", "error_type")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
