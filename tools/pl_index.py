"""poland-law index for the overlay: the Dz.U. address (DU/<year>/<position>) and the normalised title of every act
folder (from each act README), every article/paragraph file the repo has ever held keyed by its number, and the
code abbreviations courts write (k.c., k.p.c., ...) mapped to folders that exist. Writes pl_index.json.

  python pl_index.py <poland-law clone (bare is fine)> [ref]"""
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

repo = sys.argv[1]
ref = sys.argv[2] if len(sys.argv) > 2 else 'main'
# abbreviation as courts write it (normalised by abbr_key) -> act folder; kept only where the folder exists
ABBR = {
    'kc': 'kodeks-cywilny', 'kpc': 'kodeks-postepowania-cywilnego', 'kk': 'kodeks-karny', 'kpk': 'kodeks-postepowania-karnego',
    'kp': 'kodeks-pracy', 'ksh': 'kodeks-spolek-handlowych', 'op': 'ordynacja-podatkowa', 'km': 'kodeks-morski',
    'kpa': 'kodeks-postepowania-administracyjnego', 'kro': 'kodeks-rodzinny-i-opiekunczy', 'kw': 'kodeks-wykroczen',
    'kks': 'kodeks-karny-skarbowy', 'kkw': 'kodeks-karny-wykonawczy', 'ppsa': 'prawo-o-postepowaniu-przed-sadami-administracyjnymi',
    'pusa': 'prawo-o-ustroju-sadow-administracyjnych', 'usp': 'prawo-o-ustroju-sadow-powszechnych', 'pb': 'prawo-budowlane',
    'konstytucji': 'konstytucja', 'konstytucjirp': 'konstytucja', 'konstytucja': 'konstytucja', 'konstytucjarp': 'konstytucja',
}


def abbr_key(s):
    return re.sub(r'[\s.]', '', s).lower()


def norm_title(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'\(.*?\)', ' ', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s).strip()
    return s


def art_key(stem):
    """'art-022-1a' -> '22-1a'; 'art-415' -> '415'; 'par-012' -> 'par:12'."""
    kind, _, num = stem.partition('-')
    parts = [re.sub(r'^0+(?=\d)', '', p) for p in num.split('-') if p]
    k = '-'.join(parts).lower()
    return ('par:' + k) if kind == 'par' else k


def git(*a, inp=None):
    return subprocess.run(['git', '-C', repo, *a], input=inp, capture_output=True).stdout


head = git('ls-tree', '-r', '--name-only', ref).decode('utf-8').split('\n')
readmes = [p for p in head if p.count('/') == 1 and p.endswith('/README.md')]
raw = git('cat-file', '--batch', inp=''.join(f'{ref}:{p}\n' for p in readmes).encode())
du, titles, i = {}, {}, 0
for p in readmes:
    nl = raw.index(b'\n', i)
    size = int(raw[i:nl].split()[2])
    blob = raw[nl + 1:nl + 1 + size].decode('utf-8', 'replace')
    i = nl + 2 + size
    folder = p.split('/')[0]
    m = re.search(r'eli/acts/(DU|MP)/(\d{4})/(\d+)', blob)
    if m:
        du.setdefault(f'{m.group(1)}/{m.group(2)}/{m.group(3)}', folder)
    t = re.match(r'#\s*(.+)', blob)
    if t:
        nt = norm_title(t.group(1))
        titles.setdefault(nt, folder)
        short = re.sub(r'^(ustawa|rozporzadzenie|obwieszczenie|dekret|uchwala)\b.*?\br\b\s*', '', nt)
        if short and short != nt:
            titles.setdefault(short, folder)
ever = set(git('log', ref, '--format=', '--name-only', '--no-renames').decode('utf-8').split('\n')) | set(head)
arts = {}
for p in ever:
    m = re.match(r'([^/]+)/((?:art|par)-[^/]+)\.md$', p)
    if m:
        arts.setdefault(m.group(1), {})[art_key(m.group(2))] = p
abbr = {k: v for k, v in ABBR.items() if v in arts}
json.dump({'du': du, 'titles': titles, 'arts': arts, 'abbr': abbr}, open(Path(__file__).with_name('pl_index.json'), 'w', encoding='utf-8'),
          ensure_ascii=False)
print(len(du), 'acts by Dz.U. address;', len(titles), 'titles;', sum(len(v) for v in arts.values()), 'article files (ever);',
      'abbreviations kept:', sorted(abbr), '; missing:', sorted(set(ABBR) - set(abbr)))
print('DU/1964/93 ->', du.get('DU/1964/93'), '; kc 415 ->', arts.get('kodeks-cywilny', {}).get('415'),
      '; kp 22-1 ->', arts.get('kodeks-pracy', {}).get('22-1'))
