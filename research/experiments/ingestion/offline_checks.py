"""Offline research probes, NOT a production fetcher or schema. No network requests."""
from __future__ import annotations
import hashlib
import copy
import json
import platform
import sqlite3
import ssl
import sys
import tempfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
from signalnest.contracts import PageInput
from signalnest.parsing import PARSER_VERSION, parse_list

RESULTS = []

def record(name, **evidence):
    RESULTS.append(copy.deepcopy({'case': name, 'result': 'pass', **evidence}))

class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False
    def __iter__(self):
        yield from self.chunks
    def close(self):
        self.closed = True

def read_limited(client, url, limit=16):
    with client.stream('GET', url) as r:
        body = bytearray()
        for chunk in r.iter_bytes():
            if len(body) + len(chunk) > limit:
                raise ValueError('body_too_large')
            body.extend(chunk)
        return bytes(body)

def retry_after(value, received):
    if value.isascii() and value.isdigit():
        return received + int(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(received, int(date.timestamp()))
    except (ValueError, TypeError, OverflowError):
        return None

def probe_policy(client, url):
    """Small test-only state machine with fake time and tiny limits."""
    starts, seen, retries, hops, now = [], set(), 0, 0, 0.0
    while True:
        now = max(now, starts[-1] + 3 if starts else 0)
        starts.append(now)
        try:
            response = client.get(url)
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ReadError):
            if retries == 2:
                return 'exhausted', starts
            retries += 1
            now += 2 ** (retries - 1) + .25  # deterministic jitter
            continue
        if response.status_code in {301,302,303,307,308}:
            target = response.url.join(response.headers['Location'])
            if target.scheme != 'https' or target.host != 'uc.whu.edu.cn':
                return 'redirect_rejected', starts
            seen.add(str(url))
            if str(target) in seen:
                return 'redirect_loop', starts
            if hops == 3:
                return 'redirect_limit', starts
            hops += 1
            url = target
            continue
        if response.status_code == 429:
            return 'deferred', starts
        if response.status_code in {500,502,503,504}:
            if retries == 2:
                return 'exhausted', starts
            retries += 1
            now += 2 ** (retries - 1) + .25
            continue
        return ('body' if response.status_code == 200 else
                'validate' if response.status_code == 304 else 'failed'), starts

def main():
    home_url = 'https://uc.whu.edu.cn/tzgg/xstz.htm'
    old = (ROOT / 'research/fixtures/student-notices-page1.html').read_bytes()
    tree = BeautifulSoup(old, 'html.parser')
    tree.select_one('.page .p_pages').decompose()
    parsed = parse_list(PageInput(content=str(tree).encode(), page_url=home_url))
    assert len(parsed.entries) == 25 and parsed.next_page_url is None
    record('current_parser_missing_pagination', entries=25, next_page_url=None,
           meaning='reproduced defect; this pass is NOT desirable behavior')
    for name, url, rows, expected_next in [
        ('student-notices-home-20261001', home_url, 25, 'https://uc.whu.edu.cn/tzgg/xstz/23.htm'),
        ('student-notices-last-20261001', 'https://uc.whu.edu.cn/tzgg/xstz/1.htm', 13, None)]:
        raw = (ROOT / f'research/fixtures/ingestion/{name}.html').read_bytes()
        got = parse_list(PageInput(content=raw, page_url=url))
        assert len(got.entries) == rows
        assert (str(got.next_page_url) if got.next_page_url else None) == expected_next
        record(name, entries=rows, next_page_url=expected_next)

    with tempfile.TemporaryDirectory(prefix='signalnest-ingestion-') as temp:
        path = Path(temp) / 'raw.bin'
        payload = b'<html>body A</html>'
        sha = hashlib.sha256(payload).hexdigest()
        path.write_bytes(payload)
        # Deliberately independent minimal schema: proves transaction sequence only.
        dbpath = Path(temp) / 'probe.sqlite'
        db = sqlite3.connect(dbpath)
        db.executescript('CREATE TABLE evidence(id INTEGER PRIMARY KEY, status INT);'
                         'CREATE TABLE processing(raw_id INT, parser TEXT, UNIQUE(raw_id,parser));')
        with db:
            db.execute('INSERT INTO evidence VALUES(1,200)')
        try:
            with db:
                db.execute('INSERT INTO processing VALUES(1,"v1")')
                raise RuntimeError('injected_before_business_commit')
        except RuntimeError:
            pass
        assert db.execute('SELECT count(*) FROM processing').fetchone()[0] == 0
        db.close()
        db = sqlite3.connect(dbpath)
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0] == 1
        calls = []
        def unchanged(req):
            calls.append(dict(req.headers))
            assert req.headers['If-None-Match'] == '"a"'
            return httpx.Response(304, headers={'ETag': '"a"'})
        with httpx.Client(transport=httpx.MockTransport(unchanged)) as client:
            r = client.get(home_url, headers={'If-None-Match': '"a"'})
            assert r.content == b''
            assert hashlib.sha256(path.read_bytes()).hexdigest() == sha
            with db:
                db.execute('INSERT INTO evidence VALUES(2,304)')
                db.execute('INSERT INTO processing VALUES(1,"v1")')
            with db:
                db.execute('INSERT INTO processing VALUES(1,"v2")')
        assert db.execute('SELECT count(*) FROM processing').fetchone()[0] == 2
        record('archive_then_rollback_then_304_reprocess_and_parser_upgrade',
               evidence_count=2, processing_versions=['v1','v2'], reused_body_sha256=sha)
        db.close()

        for scenario in ['no_baseline', 'missing_body', 'corrupt_body']:
            if scenario == 'missing_body':
                path.unlink()
            elif scenario == 'corrupt_body':
                path.write_bytes(b'corrupt')
            seen = []
            def responses(req):
                seen.append(req.headers.get('If-None-Match'))
                return httpx.Response(304) if len(seen) == 1 else httpx.Response(200, content=payload)
            with httpx.Client(transport=httpx.MockTransport(responses)) as client:
                # Force unexpected 304, including body loss after preflight.
                first = client.get(home_url, headers={} if scenario == 'no_baseline' else {'If-None-Match':'"a"'})
                usable = scenario != 'no_baseline' and path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == sha
                assert first.status_code == 304 and not usable
                full = client.get(home_url, headers={})
                assert full.content == payload and seen[-1] is None
            record(scenario + '_304_falls_back_unconditionally', sent_validators=seen)

    # HTTPX itself carries ordinary validator headers onto redirect targets.
    seen = []
    def redirect(req):
        seen.append((str(req.url), req.headers.get('If-None-Match')))
        return httpx.Response(302, headers={'Location':'/target'}) if req.url.path != '/target' else httpx.Response(200, content=b'ok')
    with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        client.get(home_url, headers={'If-None-Match':'"home"'})
    assert seen[-1][1] == '"home"'
    record('automatic_redirect_forwards_validator', calls=seen)
    seen.clear()
    with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=False) as client:
        r = client.get(home_url, headers={'If-None-Match':'"home"'})
        target = r.url.join(r.headers['Location'])
        client.get(target, headers={})  # target has no own baseline in this probe
    assert seen[-1][1] is None
    record('manual_redirect_rebuilds_target_headers', calls=seen)

    for scenario, chunks in [('oversize',[b'12345678',b'123456789']),
                             ('read_error',[b'123'])]:
        stream = Chunks(chunks)
        if scenario == 'read_error':
            class Broken(Chunks):
                def __iter__(self):
                    yield b'123'
                    raise httpx.ReadError('injected')
            stream = Broken(chunks)
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=stream))) as client:
            try:
                read_limited(client, home_url)
                raise AssertionError('expected failure')
            except (ValueError, httpx.ReadError):
                pass
        assert stream.closed
        record(scenario+'_closes_stream_no_complete_body')

    now = 1790841600
    values = ['120','Thu, 01 Oct 2026 08:02:00 GMT','Wed, 30 Sep 2026 08:00:00 GMT','-1','1.5','bad']
    due = [retry_after(v, now) for v in values]
    assert due == [now+120,now+120,now,None,None,None]
    assert due[0] - now > 15  # wait ceiling15 => DEFER; never truncate to15
    record('retry_after_seconds_dates_invalid_and_deferral', values=values, due=due)

    # Fake clock verifies policy arithmetic; it is not real socket timing.
    t, starts = 0.0, []
    for earliest in [0.0,1.2,2.4,120.0]:
        t = max(t, earliest, (starts[-1]+3 if starts else 0))
        starts.append(t)
    assert starts == [0,3,6,120]
    record('shared_spacing_for_initial_redirect_retry_fake_clock', starts=starts)
    for label, sequence, outcome, count in [
        ('read_timeout_then_200',[httpx.ReadTimeout('injected'),200],'body',2),
        ('three_503',[503,503,503],'exhausted',3),
        ('403_no_retry',[403],'failed',1),
        ('429_no_immediate_retry',[429],'deferred',1),
        ('501_no_retry',[501],'failed',1),
        ('304_validation_branch',[304],'validate',1)]:
        remaining = iter(sequence)
        def handler(req):
            value = next(remaining)
            if isinstance(value, Exception):
                raise value
            return httpx.Response(value, content=b'ok' if value == 200 else b'')
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result, starts = probe_policy(client, home_url)
        assert result == outcome and len(starts) == count
        assert all(b-a >= 3 for a,b in zip(starts,starts[1:]))
        record(label, outcome=result, request_starts=starts, scope='test-only proposed policy')
    for label, location, expected in [
        ('cross_origin','https://other.example/','redirect_rejected'),
        ('downgrade','http://uc.whu.edu.cn/','redirect_rejected'),
        ('loop',home_url,'redirect_loop'),
        ('too_many_hops',None,'redirect_limit')]:
        def redirects(req):
            target = location or str(req.url) + 'x'
            return httpx.Response(302, headers={'Location':target})
        with httpx.Client(transport=httpx.MockTransport(redirects)) as client:
            result, starts = probe_policy(client, home_url)
        assert result == expected
        record('manual_redirect_'+label, outcome=result, requests=len(starts))
    def fails(req):
        raise httpx.ConnectError('TLS', request=req) from ssl.SSLCertVerificationError('injected')
    with httpx.Client(transport=httpx.MockTransport(fails)) as client:
        try:
            client.get(home_url)
        except httpx.ConnectError as exc:
            assert isinstance(exc.__cause__, ssl.SSLCertVerificationError)
        else:
            raise AssertionError('missing exception')
    record('injected_tls_cause_identifiable', scope='not a real handshake')
    output = {'run_at_utc':datetime.now(timezone.utc).isoformat(),
              'environment':{'python':sys.version,'platform':platform.platform(),
                             'httpx':httpx.__version__,'parser_version':PARSER_VERSION},
              'results':RESULTS}
    print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
