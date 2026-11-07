"""Stage 1: every SAOS dump page -> per-group gzip JSONL parts (parallel by page): the fields the build writes, the
judgment text as plain text, and the court name resolved from the court lists (common courts by division, Supreme
Court chambers, administrative courts from the case number).

  python parse.py <cache dir with pages/ and courts/> <out dir> [--workers 20]

Groups: sad-najwyzszy, trybunal-konstytucyjny, sady-administracyjne, krajowa-izba-odwolawcza, and the common courts by
appellate region: sady-powszechne-<appeal court city>."""
from __future__ import annotations

import argparse
import gzip
import html
import json
import re
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

TAG = re.compile(r'<[^>]+>')
BLOCK = re.compile(r'</(p|div|h\d|li|tr|table|blockquote)>|<br\s*/?>', re.I)
SRC_DATE = re.compile(r'_(\d{4}-\d{2}-\d{2})_\d{3}$')
TODAY = date.today().isoformat()
_COURTS: dict = {}
# the appellate courts' seats, from the locative of their names ("Sąd Apelacyjny we Wrocławiu") to the city's name
NOMINATIVE = {'Wrocławiu': 'Wrocław', 'Białymstoku': 'Białystok', 'Gdańsku': 'Gdańsk', 'Katowicach': 'Katowice',
              'Krakowie': 'Kraków', 'Łodzi': 'Łódź', 'Lublinie': 'Lublin', 'Poznaniu': 'Poznań', 'Rzeszowie': 'Rzeszów',
              'Szczecinie': 'Szczecin', 'Warszawie': 'Warszawa'}


def slug(s: str) -> str:
    s = unicodedata.normalize('NFKD', s.replace('ł', 'l').replace('Ł', 'L')).encode('ascii', 'ignore').decode()
    return re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-').lower() or 'x'


def text_of(h: str) -> str:
    t = html.unescape(TAG.sub('', BLOCK.sub('\n', h or '')))
    lines = [l.strip() for l in t.replace('\r', '').split('\n')]
    out, blank = [], False
    for l in lines:
        if not l:
            blank = bool(out)
            continue
        if blank:
            out.append('')
        out.append(l)
        blank = False
    return '\n'.join(out)


def courts(cache: str) -> dict:
    if _COURTS:
        return _COURTS
    cc = json.load(open(Path(cache) / 'courts' / 'commonCourts.json', encoding='utf-8'))
    sc = json.load(open(Path(cache) / 'courts' / 'scChambers.json', encoding='utf-8'))
    by_id = {c['id']: c for c in cc}

    def appeal_of(c):
        seen = 0
        while c.get('parentCourt') and seen < 5:
            c = by_id.get(c['parentCourt']['id'], c)
            seen += 1
        return c

    div = {}
    for c in cc:
        top = appeal_of(c) if c.get('type') != 'APPEAL' else c
        region = re.sub(r'^Sąd Apelacyjny (w|we)\s+', '', top['name'])
        region = NOMINATIVE.get(region, region)
        for d in c.get('divisions', []):
            div[d['id']] = {'court': c['name'], 'division': d['name'], 'region': region}
    scdiv = {}
    for ch in sc:
        for d in ch.get('divisions', []):
            scdiv[d['id']] = {'court': 'Sąd Najwyższy', 'chamber': ch['name'], 'division': d['name']}
        scdiv.setdefault(('chamber', ch['id']), {'court': 'Sąd Najwyższy', 'chamber': ch['name']})
    _COURTS.update({'common': div, 'sc': scdiv})
    return _COURTS


def admin_court(case: str) -> str:
    """'II OSK 123/20' -> Naczelny Sąd Administracyjny; 'II SA/Wa 123/20' -> Wojewódzki Sąd Administracyjny (Wa)."""
    if re.search(r'\b[OFG]SK\b|\bO[PZ]S\b|\b[OFG]Z\b|\bFPS\b|\bGPS\b|\bOPS\b', case or ''):
        return 'Naczelny Sąd Administracyjny'
    m = re.search(r'SA[B]?/(\w+)', case or '')
    return f'Wojewódzki Sąd Administracyjny ({m.group(1)})' if m else 'Sąd administracyjny'


def record(j: dict, cache: str) -> tuple[str, dict] | None:
    text = text_of(j.get('textContent') or '')
    if not text:
        return None
    ct = j.get('courtType')
    cases = [c.get('caseNumber') for c in j.get('courtCases') or [] if c.get('caseNumber')]
    divid = (j.get('division') or {}).get('id')
    C = courts(cache)
    court, division, chamber = None, None, None
    if ct == 'COMMON':
        d = C['common'].get(divid, {})
        court, division = d.get('court'), d.get('division')
        group = 'sady-powszechne-' + slug(d.get('region') or 'inne')
    elif ct == 'SUPREME':
        d = C['sc'].get(divid, {})
        court, division, chamber = 'Sąd Najwyższy', d.get('division'), d.get('chamber')
        group = 'sad-najwyzszy'
    elif ct == 'ADMINISTRATIVE':
        court = admin_court(cases[0] if cases else '')
        group = 'sady-administracyjne'
    elif ct == 'CONSTITUTIONAL_TRIBUNAL':
        court, group = 'Trybunał Konstytucyjny', 'trybunal-konstytucyjny'
    elif ct == 'NATIONAL_APPEAL_CHAMBER':
        court, group = 'Krajowa Izba Odwoławcza', 'krajowa-izba-odwolawcza'
    else:
        court, group = ct or 'Sąd', 'inne'
    day = (j.get('judgmentDate') or '')[:10]
    src = j.get('source') or {}
    if not re.match(r'\d{4}-\d{2}-\d{2}$', day) or day < '1918-01-01' or day > TODAY:
        m = SRC_DATE.search(src.get('judgmentId') or '')  # common-court ids end with the judgment day
        day = m.group(1) if m and '1918-01-01' <= m.group(1) <= TODAY else None
    regs = []
    for r in j.get('referencedRegulations') or []:
        regs.append({'title': r.get('journalTitle') or '', 'year': r.get('journalYear'), 'no': r.get('journalNo'),
                     'entry': r.get('journalEntry'), 'text': r.get('text') or ''})
    judges = [g.get('name') for g in j.get('judges') or [] if g.get('name')]
    return group, {
        'saos_id': j['id'], 'court_type': ct, 'court': court, 'chamber': chamber, 'division': division, 'cases': cases,
        'decided': day, 'judgment_type': j.get('judgmentType'), 'judges': judges, 'source_url': src.get('judgmentUrl'),
        'source_code': src.get('code'), 'publication_date': src.get('publicationDate'), 'summary': text_of(j.get('summary') or ''),
        'decision': text_of(j.get('decision') or ''), 'keywords': j.get('keywords') or [], 'regulations': regs, 'text': text,
        'modified': j.get('modificationDate') or src.get('publicationDate')}


def one(page: str, cache: str, out: str) -> dict:
    items = json.load(gzip.open(page, 'rt', encoding='utf-8'))
    by = {}
    skipped = 0
    for j in items:
        r = record(j, cache)
        if not r:
            skipped += 1
            continue
        by.setdefault(r[0], []).append(r[1])
    stem = Path(page).name.split('.')[0]
    for g, rows in by.items():
        d = Path(out) / g
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f'{stem}.part'
        with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=3) as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        tmp.replace(d / f'{stem}.jsonl.gz')
    return {g: len(v) for g, v in by.items()} | {'_no_text': skipped}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cache'), ap.add_argument('out'), ap.add_argument('--workers', type=int, default=20)
    a = ap.parse_args()
    pages = sorted(str(p) for p in Path(a.cache, 'pages').glob('*.json.gz'))
    tot = {}
    with ProcessPoolExecutor(a.workers) as ex:
        for s in ex.map(one, pages, [a.cache] * len(pages), [a.out] * len(pages), chunksize=8):
            for k, v in s.items():
                tot[k] = tot.get(k, 0) + v
    Path(a.out, 'stats.json').write_text(json.dumps(tot, indent=1, ensure_ascii=False))
    print(json.dumps(dict(sorted(tot.items(), key=lambda kv: -kv[1])), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
