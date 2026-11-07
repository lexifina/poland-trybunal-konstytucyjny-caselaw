"""Fetch the SAOS judgment dump (saos.org.pl /api/dump/judgments, pages of 100) plus the court and division lists,
politely (at most RATE requests a second, many in flight), resumable: a page already saved is skipped.

  python fetch.py <out dir> [--rate 8] [--since YYYY-MM-DD]   (--since: sinceModificationDate, for nightly deltas)

Writes <out>/pages/<N>.json.gz (the page's items, as SAOS returns them) and <out>/courts/<kind>.json."""
import argparse
import gzip
import json
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = 'lexifina-gitlaw (case law archive)'
BASE = 'https://www.saos.org.pl/api/dump/'
ap = argparse.ArgumentParser()
ap.add_argument('out'), ap.add_argument('--rate', type=float, default=8), ap.add_argument('--since', default='')
ap.add_argument('--threads', type=int, default=32)
a = ap.parse_args()
out = Path(a.out)
(out / 'pages').mkdir(parents=True, exist_ok=True)
(out / 'courts').mkdir(parents=True, exist_ok=True)
lock = threading.Lock()
nxt = [time.time()]


def get(url: str) -> dict:
    with lock:
        wait = nxt[0] - time.time()
        nxt[0] = max(nxt[0], time.time()) + 1 / a.rate
    if wait > 0:
        time.sleep(wait)
    err = None
    for i in range(8):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            return json.load(urllib.request.urlopen(req, timeout=300))
        except Exception as e:  # transient: back off
            err = e
            time.sleep(5 * (i + 1))
    raise err


def dump(kind: str, size: int = 100) -> list:
    items, n = [], 0
    while True:
        d = get(f'{BASE}{kind}?' + urllib.parse.urlencode({'pageSize': size, 'pageNumber': n}))
        got = d.get('items', [])
        items += got
        if len(got) < size:
            return items
        n += 1


for kind in ('commonCourts', 'scChambers', 'enrichments' if False else None):
    if kind and not (out / 'courts' / f'{kind}.json').exists():
        (out / 'courts' / f'{kind}.json').write_text(json.dumps(dump(kind), ensure_ascii=False), encoding='utf-8')
        print('saved', kind, flush=True)

params = {'pageSize': 100, 'withGenerated': 'true'}
if a.since:
    params['sinceModificationDate'] = a.since + 'T00:00:00.000'


def page(n: int):
    p = out / 'pages' / f'{n:05d}.json.gz'
    if p.exists():
        return n, -1
    d = get(f'{BASE}judgments?' + urllib.parse.urlencode({**params, 'pageNumber': n}))
    items = d.get('items', [])
    tmp = p.with_suffix('.part')
    with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=4) as f:
        json.dump(items, f, ensure_ascii=False)
    tmp.replace(p)
    return n, len(items)


# find the end first (binary search on empty pages), then fetch every page in parallel
lo, hi = 0, 1
while len(get(f'{BASE}judgments?' + urllib.parse.urlencode({**params, 'pageNumber': hi * 512})).get('items', [])) == 100:
    hi *= 2
lo, hi = (hi // 2) * 512, hi * 512
while hi - lo > 1:
    mid = (lo + hi) // 2
    if get(f'{BASE}judgments?' + urllib.parse.urlencode({**params, 'pageNumber': mid})).get('items'):
        lo = mid
    else:
        hi = mid
last = lo
print('pages 0 ..', last, flush=True)
done = [0]


def safe(n):
    try:
        return page(n)
    except Exception as e:  # a page SAOS cannot serve right now: retried below, never fatal
        return n, f'failed: {e!r}'[:200]


todo = list(range(last + 1))
for attempt in range(4):
    failed = []
    with ThreadPoolExecutor(a.threads) as ex:
        for n, k in ex.map(safe, todo):
            done[0] += 1
            if isinstance(k, str):
                failed.append(n)
            if done[0] % 250 == 0:
                print('fetched', done[0], flush=True)
    if not failed:
        break
    print(f'pass {attempt + 1}: {len(failed)} pages failed, retrying', flush=True)
    todo = failed
    time.sleep(60)
if failed:
    (out / 'FAILED').write_text('\n'.join(map(str, failed)))
    print('pages still failing:', failed[:20], flush=True)
(out / 'FETCHED').write_text(json.dumps({'pages': last + 1, 'since': a.since, 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}))
print('DONE', last + 1, flush=True)
