"""Stage 2: one Polish case-law repo from the parsed SAOS parts, straight into git fast-import.

  python build.py <group> <parsed dir> <out repo dir> --today YYYY-MM-DD --fetched YYYY-MM-DD

One commit per judgment (Kind: court), dated the day given (noon Warsaw); then one Lexifina documentation commit with
README, REPRODUCE.md, corpus.json, decision-index.tsv (case number -> path), unresolved.csv and the judgment overlay
judgments/poland/national/acts/<act>/<art>.jsonl, each row pinned to the cited article's own version in poland-law on the
day given (reconcile.fix_row). Deterministic for the same input."""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import re
import subprocess
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import regex

HERE = Path(__file__).resolve().parent
_rs = importlib.util.spec_from_file_location('pl_reconcile', HERE.parent / 'nl-caselaw' / 'reconcile.py')
RC = importlib.util.module_from_spec(_rs)
_rs.loader.exec_module(RC)

LAW_CORPUS = 'poland/national/acts'
WARSAW = timezone(timedelta(hours=1))
COMMITTER = b'Lexifina <corpus@lexifina.com>'
EPOCH_NOON = 11 * 3600
ISO = re.compile(r'^\d{4}-\d{2}-\d{2}$')
NAMES = {'sad-najwyzszy': 'the Sąd Najwyższy (Supreme Court)', 'trybunal-konstytucyjny': 'the Trybunał Konstytucyjny (Constitutional Tribunal)',
         'sady-administracyjne': 'the administrative courts (Naczelny Sąd Administracyjny and the wojewódzkie sądy administracyjne)',
         'krajowa-izba-odwolawcza': 'the Krajowa Izba Odwoławcza (National Appeal Chamber)'}
SUP = str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹', '0123456789')
# "art. 415 k.c.", "art. 22¹ § 1 k.p.", "art. 6 ust. 1 ustawy ...", "art. 77[1] k.p.", "art. 32 Konstytucji RP"
ART = r'(\d+)\s*(?:[⁰¹²³⁴⁵⁶⁷⁸⁹]+|\[\d+\]|\^\d+)?\s*([a-z]{0,2})(?![a-z])'
CITE = regex.compile(r'\bart(?:ykuł|\.)?\s*' + ART + r'((?:\s*(?:§|ust\.|pkt|zd\.|lit\.)\s*[\w\[\]]+(?:\s*(?:i|oraz|,)\s*\d+[a-z]?)*)*)\s*'
                     r'(k\.\s?c\.|k\.\s?p\.\s?c\.|k\.\s?k\.|k\.\s?p\.\s?k\.|k\.\s?p\.|k\.\s?s\.\s?h\.|o\.\s?p\.|k\.\s?m\.|p\.\s?u\.\s?s\.\s?a\.|'
                     r'Konstytucji(?:\s+RP)?|Konstytucja(?:\s+RP)?|kc|kpc|kk|kpk|kp|ksh)(?![\w])', regex.I)
ART_START = regex.compile(r'\bart(?:ykuł|\.)', regex.I)
REG_ARTS = regex.compile(r'art\.\s*' + ART, regex.I)


def slug(s: str) -> str:
    s = unicodedata.normalize('NFKD', s.replace('ł', 'l').replace('Ł', 'L')).encode('ascii', 'ignore').decode()
    return re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-')


def email_local(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:60] or 'judgment'


def art_key(num: str, sup: str, letters: str) -> str:
    k = str(int(num))
    if sup:
        k += '-' + str(int(sup))
    return k + (letters or '').lower()


def sup_of(raw: str) -> str:
    m = re.search(r'([⁰¹²³⁴⁵⁶⁷⁸⁹]+)|\[(\d+)\]|\^(\d+)', raw)
    if not m:
        return ''
    return (m.group(1) or '').translate(SUP) or m.group(2) or m.group(3) or ''


def norm_title(s):  # pl_index.py's rule
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'\(.*?\)', ' ', s)
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


class Resolver:
    def __init__(self, idx):
        self.du, self.titles, self.arts, self.abbr = idx['du'], idx['titles'], idx['arts'], idx['abbr']
        self.timeouts = 0

    def folder_of_reg(self, r) -> str | None:
        if r.get('year') and r.get('entry'):
            f = self.du.get(f"DU/{r['year']}/{r['entry']}")
            if f:
                return f
        t = norm_title(r.get('title') or '')
        if t in self.titles:
            return self.titles[t]
        short = re.sub(r'^(ustawa|rozporzadzenie|obwieszczenie|dekret|uchwala)\b.*?\br\b\s*', '', t)
        return self.titles.get(short)

    def path(self, folder, key):
        return self.arts.get(folder, {}).get(key) if folder else None

    def from_regs(self, regs) -> tuple[list, int]:
        out, miss = [], 0
        for r in regs:
            folder = self.folder_of_reg(r)
            tail = r.get('text') or ''  # the article list follows the address, inside the closing parenthesis
            hits = 0
            for m in REG_ARTS.finditer(tail[:2000], timeout=0.05) if folder else ():
                p = self.path(folder, art_key(m.group(1), sup_of(m.group(0)), m.group(2)))
                if p:
                    out.append((p, re.sub(r'\s+', ' ', f"{r.get('title') or ''} - {m.group(0)}").strip()[:200]))
                    hits += 1
            if not hits:
                miss += 1
        return out, miss

    def from_text(self, text) -> list:
        out = []
        for s in ART_START.finditer(text or ''):
            try:
                m = CITE.match(text[s.start():s.start() + 300], timeout=0.05)
            except TimeoutError:
                self.timeouts += 1
                continue
            if not m:
                continue
            folder = self.abbr.get(re.sub(r'[\s.]', '', m.group(4)).lower())
            p = self.path(folder, art_key(m.group(1), sup_of(m.group(0)), m.group(2)))
            if p:
                out.append((p, re.sub(r'\s+', ' ', m.group(0)).strip()[:200]))
        return out


def render(r, day, res, ver, extra=()):
    court = r['court'] or 'Sąd'
    case = r['cases'][0] if r['cases'] else f"SAOS {r['saos_id']}"
    path = f"decisions/{slug(court).lower()[:50]}/{day[:4]}/{slug(case) or r['saos_id']}-{r['saos_id']}.md"
    cites, miss = res.from_regs(r['regulations'])
    cites += res.from_text(r['text'])
    # A judgment cannot apply an article that did not exist yet: "art. 1 Konstytucji" in 1991 is the 1952 Constitution,
    # not the 1997 one poland-law holds. poland-law keeps each act from its first publication, so a target whose record
    # starts after the day given is another act of the same name; the link is dropped (counted in unresolved.csv).
    kept = [(p, how) for p, how in cites if not ver.get(p) or ver[p][0][0] <= day]
    miss += len(cites) - len(kept)
    cites = kept
    url = r['source_url'] or f"https://www.saos.org.pl/judgments/{r['saos_id']}"
    fm = {'saos_id': r['saos_id'], 'court': court, 'chamber': r['chamber'], 'division': r['division'], 'sygnatura': case,
          'other_case_numbers': r['cases'][1:] or None, 'decided': day, 'judgment_type': r['judgment_type'],
          'judges': r['judges'] or None, 'source_url': url, 'saos_url': f"https://www.saos.org.pl/judgments/{r['saos_id']}",
          'keywords': r['keywords'] or None}
    lines = ['---'] + [f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in fm.items() if v not in (None, '')]
    uniq = sorted({p for p, _ in cites})
    if uniq:
        lines.append('cites_law: ' + json.dumps([f'{LAW_CORPUS}/{p}' for p in uniq], ensure_ascii=False))
    jt = {'SENTENCE': 'Wyrok', 'DECISION': 'Postanowienie', 'RESOLUTION': 'Uchwała', 'REASONS': 'Uzasadnienie',
          'REGULATION': 'Zarządzenie'}.get(r['judgment_type'] or '', 'Orzeczenie')
    lines += ['---', '', f'# {court}, {jt}, {day}, {case}', '']
    if r['summary']:
        lines += ['## Teza', '', r['summary'], '']
    if r['decision']:
        lines += ['## Rozstrzygnięcie', '', r['decision'], '']
    lines += ['## Treść', '', r['text'].strip(), '']
    body = '\n'.join(lines).encode('utf-8')
    author = f'{court} {case}'.replace('<', '').replace('>', '')[:200]
    email = f"{email_local(case)}-{r['saos_id']}@caselaw.invalid"
    kind = 'correction' if any(t.startswith('Corrected:') for t in extra) else 'court'
    tr = [f'Kind: {kind}', f'In-force: {day}', f'Decided: {day}', f'Court: {court}', f'Docket: {case}', f"SAOS: {r['saos_id']}",
          f'Source: {url}']
    seen = set()
    for p, how in cites:
        if p not in seen and len(seen) < 60:
            seen.add(p)
            tr.append(f'Cites: {LAW_CORPUS}/{p} ({how[:120]})')
    tr += [t for t in extra if t and not t.rstrip().endswith(':')]
    msg = (f'{court} {case}: {jt}'[:200] + '\n\n' + '\n'.join(tr) + '\n').encode('utf-8')
    counts = {}
    for p, _ in cites:
        counts[p] = counts.get(p, 0) + 1
    rows, done = [], set()
    for p, how in cites:
        if p in done:
            continue
        done.add(p)
        row = {'decided': day, 'name': f'{court} {case}', 'court': court, 'citation': case, 'url': url, 'how': how,
               'mentions': counts[p], 'decision': path}
        rows.append((p, RC.fix_row(row, p, ver)))
    refs = sorted({*r['cases'], f"SAOS {r['saos_id']}"})
    return path, body, author, email, msg, rows, refs, miss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('group'), ap.add_argument('parsed'), ap.add_argument('out')
    ap.add_argument('--today', required=True), ap.add_argument('--fetched', required=True)
    ap.add_argument('--law-repo', default=str(Path.home() / 'Documents' / 'caselaw-law-mirrors' / 'poland-law.git'))
    ap.add_argument('--chunk-bytes', type=int, default=2_500_000_000)
    a = ap.parse_args()
    out = Path(a.out)
    res = Resolver(json.load(open(HERE / 'pl_index.json', encoding='utf-8')))
    ver = RC.versions(Path(a.law_repo), ref='main')
    recs = {}
    for f in sorted((Path(a.parsed) / a.group).glob('*.jsonl.gz')):
        with gzip.open(f, 'rt', encoding='utf-8') as fh:
            for line in fh:
                r = json.loads(line)
                recs[r['saos_id']] = r
    recs = sorted(recs.values(), key=lambda r: (r['decided'] or '9999', r['court'] or '', r['cases'][:1], r['saos_id']))
    print(f'{a.group}: {len(recs)} judgments', file=sys.stderr, flush=True)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'{out} is not empty')
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(out)], check=True)
    fi = subprocess.Popen(['git', '-C', str(out), 'fast-import', '--quiet', '--done', f'--export-marks={out / ".git" / "marks"}',
                          '--big-file-threshold=64m'], stdin=subprocess.PIPE)
    w = fi.stdin.write
    overlay, dindex, undated, checkpoints = {}, [], [], []
    mark = raw = clamp = miss = 0
    first = None
    for r in recs:
        day = r['decided']
        if not day or not ISO.match(day) or day > a.today:
            undated.append(r)
            continue
        first = first or day
        path, body, author, email, msg, rows, refs, m = render(r, day, res, ver)
        miss += m
        dindex += [x + '\t' + path for x in refs]
        raw += len(body)
        d0 = date.fromisoformat(day)
        if d0.year < 1970:
            clamp += 1
            ts = EPOCH_NOON + clamp - 3600
        else:
            ts = int(datetime(d0.year, d0.month, d0.day, 12, tzinfo=WARSAW).timestamp())
        mark += 1
        w(b'commit refs/heads/main\nmark :%d\n' % mark)
        w(b'author %s <%s> %d +0100\ncommitter %s %d +0100\n' % (author.encode('utf-8'), email.encode(), ts, COMMITTER, ts))
        w(b'data %d\n%s\n' % (len(msg), msg))
        w(b'M 100644 inline %s\ndata %d\n%s\n' % (path.encode('utf-8'), len(body), body))
        if raw >= (len(checkpoints) + 1) * a.chunk_bytes:
            checkpoints.append(mark)
        for p, row in rows:
            overlay.setdefault(p, []).append(row)
    docs = {}
    for p, rows in overlay.items():
        rows.sort(key=lambda x: (x['decided'], x['citation'], x['decision']))
        docs[f'judgments/{LAW_CORPUS}/{p[:-3]}.jsonl'] = ''.join(json.dumps(x, ensure_ascii=False, sort_keys=True) + '\n' for x in rows)
    for r in undated:
        case = r['cases'][0] if r['cases'] else f"SAOS {r['saos_id']}"
        docs[f"decisions/undated/{slug(case)}-{r['saos_id']}.md"] = (
            f"---\nsaos_id: {json.dumps(r['saos_id'])}\ncourt: {json.dumps(r['court'], ensure_ascii=False)}\nsygnatura: {json.dumps(case, ensure_ascii=False)}\n"
            f"source_url: {json.dumps(r['source_url'])}\n---\n\n# {r['court']}, {case}\n\n{r['text']}\n")
    docs['decision-index.tsv'] = 'reference' + chr(9) + 'path' + chr(10) + ''.join(x + chr(10) for x in sorted(set(dindex)))
    n_refs = sum(len(v) for v in overlay.values())
    cities = {slug(c).lower(): c for c in ('Wrocław', 'Białystok', 'Gdańsk', 'Katowice', 'Kraków', 'Łódź', 'Lublin', 'Poznań',
                                          'Rzeszów', 'Szczecin', 'Warszawa')}
    city = cities.get(a.group.removeprefix('sady-powszechne-'))
    where = NAMES.get(a.group) or (f'the common courts (sądy powszechne) of the {city} appellate region' if city
                                   else 'the common courts (sądy powszechne) with no appellate region recorded')
    gl_path = f'poland/{a.group}/caselaw'
    src = {'publisher': 'SAOS, System Analizy Orzeczeń Sądowych (saos.org.pl), from the courts\' own portals',
           'url': 'https://www.saos.org.pl/api/dump/judgments', 'fetched': a.fetched,
           'licence': 'Judgments are official documents, not subject to copyright (Art. 4 of the Polish Copyright Act); SAOS states no reuse conditions'}
    docs['corpus.json'] = json.dumps({
        'corpus': f'pl/{a.group}/caselaw', 'path': gl_path, 'name': f'Case law: Poland, {where}', 'label': f'Poland case law ({where})',
        'language': 'pl', 'kind': 'judgments', 'history': 'in-force',
        'coverage': f'{mark} judgments with text from SAOS, one file and one commit each, dated the day given; {n_refs} citations of law linked to poland-law.',
        'history_from': first, 'record_to': a.fetched, 'checked_on': a.today,
        'record_note': 'Court decisions, not laws: every commit is Kind: court. In-force is the day given. Judgments with no usable date are in decisions/undated/.',
        'in_force_trailer': 'In-force', 'correction_trailer': 'Corrected', 'dates': {'commit': 'decided', 'time': '12:00 Europe/Warsaw'},
        'source': src, 'overlay': {'path': f'judgments/{LAW_CORPUS}/<act>/<art>.jsonl', 'law_corpus': LAW_CORPUS}}, ensure_ascii=False, indent=2) + '\n'
    docs['README.md'] = f"""# Polish case law: {where}

Part of [lexifina/global-law](https://github.com/lexifina/global-law), one git history for every country's law: this is the case law of {where}, Poland, which global-law reads at `{gl_path}/`.

Every judgment of {where} that SAOS (System Analizy Orzeczeń Sądowych) holds with its text, as a git history: one file per
judgment, one commit per judgment, dated the day it was given. {mark:,} judgments from {first} to {a.fetched}.

## Questions as git commands

| Question | Command |
|---|---|
| A judgment | `cat decisions/<court>/<year>/<sygnatura>-<saos id>.md` |
| By case number | `grep -P '^II CSK 123/20\\t' decision-index.tsv` · `git log --grep='^Docket: II CSK 123/20$' --stat` |
| Judgments given in a window | `git log --since=2024-03-01 --until=2024-03-31 --format='%ad %an' --date=short` |
| Judgments applying art. 415 k.c. | `cat judgments/poland/national/acts/kodeks-cywilny/art-415.jsonl` |
| The article as the court applied it | each overlay row's `law_commit` is the article's own version in poland-law on the day given (`law_record_from` when the judgment is older than the record) |
| Who wrote a line? | `git blame decisions/...`: each line is credited to the judgment (court and case number) |

## Dates and authors
The commit date is the day the judgment was given, at noon Warsaw (`In-force:` and `Decided:`). The author is the court and
the case number (sygnatura), at an address that cannot exist. These are court rulings, not laws: every commit is
`Kind: court`.

## The judgment overlay
Links come from the courts' own list of cited provisions (SAOS `referencedRegulations`, by Dziennik Ustaw address) and
from citations in the text ("art. 415 k.c.", "art. 22¹ § 1 k.p.", "art. 32 Konstytucji RP"), kept only where they land on
an article file poland-law holds. {n_refs:,} are linked.

## Source and licence
The judgments come from SAOS (https://www.saos.org.pl), which gathers them from the courts' own portals (orzeczenia.ms.gov.pl,
sn.pl, trybunal.gov.pl, orzeczenia.nsa.gov.pl, uzp.gov.pl); each file links to its source. Court judgments are official
documents and are not subject to copyright under Art. 4 of the Polish Copyright Act (ustawa o prawie autorskim i prawach
pokrewnych); SAOS states no conditions on reuse. Anonymisation is the courts' own. The arrangement of this repository is
dedicated to the public domain under CC0 1.0.

## What it does not show
Judgments the courts do not publish, and links to acts poland-law does not yet hold (counted in unresolved.csv).
"""
    docs['unresolved.csv'] = 'cited regulations with no article linked\n' + str(miss) + '\n'
    docs['REPRODUCE.md'] = f"""# Reproducing this repository

Input: the SAOS judgment dump (https://www.saos.org.pl/api/dump/judgments, pages of 100, withGenerated=true) fetched on
{a.fetched} by `tools/fetch.py`, parsed by `tools/parse.py`; poland-law `main` for the overlay (`tools/pl_index.py`).

```
python tools/fetch.py <cache>
python tools/parse.py <cache> <parsed>
python tools/build.py {a.group} <parsed> <empty folder> --today {a.today} --fetched {a.fetched}
```

Gives {mark:,} judgment commits and one documentation commit.
"""
    for f in ('fetch.py', 'parse.py', 'build.py', 'pl_index.py'):
        docs[f'tools/{f}'] = (HERE / f).read_text(encoding='utf-8')
    ts = int(datetime.fromisoformat(a.today + 'T12:00:00+01:00').timestamp())
    msg = f'Documentation: README, REPRODUCE, corpus.json, decision index and the judgment overlay ({n_refs} citations)\n'.encode()
    w(b'commit refs/heads/main\nauthor Lexifina <corpus@lexifina.com> %d +0100\ncommitter %s %d +0100\n' % (ts, COMMITTER, ts))
    w(b'data %d\n%s\n' % (len(msg), msg))
    for p in sorted(docs):
        b = docs[p].encode('utf-8')
        w(b'M 100644 inline %s\ndata %d\n%s\n' % (p.encode('utf-8'), len(b), b))
    w(b'done\n')
    fi.stdin.close()
    if fi.wait():
        raise SystemExit('fast-import failed')
    marks = dict(l.split() for l in (out / '.git' / 'marks').read_text().splitlines())
    (out / '.git' / 'push-checkpoints').write_text(''.join(marks[f':{m}'] + '\n' for m in checkpoints))
    print(json.dumps({'group': a.group, 'decisions': mark, 'undated': len(undated), 'overlay_rows': n_refs, 'regs_unlinked': miss,
                      'regex_timeouts': res.timeouts, 'raw_gb': round(raw / 1e9, 2), 'checkpoints': len(checkpoints)}), flush=True)


if __name__ == '__main__':
    main()
