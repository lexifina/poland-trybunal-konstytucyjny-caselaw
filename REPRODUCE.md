# Reproducing this repository

Input: the SAOS judgment dump (https://www.saos.org.pl/api/dump/judgments, pages of 100, withGenerated=true) fetched on
2026-11-07 by `tools/fetch.py`, parsed by `tools/parse.py`; poland-law `main` for the overlay (`tools/pl_index.py`).

```
python tools/fetch.py <cache>
python tools/parse.py <cache> <parsed>
python tools/build.py trybunal-konstytucyjny <parsed> <empty folder> --today 2026-11-07 --fetched 2026-11-07
```

Gives 9,503 judgment commits and one documentation commit.
