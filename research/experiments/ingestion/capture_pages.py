"""Explicit, finite public-page capture. Run only deliberately; never follows redirects."""
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'research/fixtures/ingestion'
URLS = [('student-notices-home-20261001', 'https://uc.whu.edu.cn/tzgg/xstz.htm'),
        ('student-notices-last-20261001', 'https://uc.whu.edu.cn/tzgg/xstz/1.htm')]
HEADERS = {'User-Agent': 'SignalNest-source-research/0.1 (limited public page inspection)',
           'Accept-Encoding': 'identity'}

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, _ in URLS:
        if any((OUT / (name + suffix)).exists() for suffix in ('.html', '.json')):
            raise RuntimeError('fixture exists; choose new filenames before recapturing')
    with httpx.Client(verify=True, trust_env=False, follow_redirects=False,
                      timeout=httpx.Timeout(10, connect=5), headers=HEADERS) as client:
        for index, (name, url) in enumerate(URLS):
            if index:
                time.sleep(4)
            started = datetime.now(timezone.utc).isoformat()
            with client.stream('GET', url) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise RuntimeError('body too large')
                metadata = {'requested_url': url, 'final_url': str(response.url),
                            'started_at_utc': started,
                            'completed_at_utc': datetime.now(timezone.utc).isoformat(),
                            'status_code': response.status_code, 'bytes': len(data),
                            'sha256': hashlib.sha256(data).hexdigest(),
                            'request_headers': HEADERS,
                            'response_headers': {k: v for k, v in response.headers.items()
                                                 if k in {'date', 'content-type', 'content-length',
                                                          'etag', 'last-modified', 'cache-control',
                                                          'content-encoding', 'vary', 'location'}},
                            'header_policy': 'allowlist only; cookies and other headers omitted',
                            'body_encoding': 'decoded content bytes from HTTPX iter_bytes'}
                if response.status_code != 200:
                    raise RuntimeError(f'HTTP {response.status_code}; no fixture saved')
                (OUT / (name + '.html')).write_bytes(data)
                (OUT / (name + '.json')).write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+'\n')
                soup = BeautifulSoup(data, 'html.parser')
                print(json.dumps(metadata, ensure_ascii=False))
                print(soup.select_one('.page .p_pages'))
                print('rows:', len(soup.select('div.list_txt > ul.am-list > li')))

if __name__ == '__main__':
    main()
