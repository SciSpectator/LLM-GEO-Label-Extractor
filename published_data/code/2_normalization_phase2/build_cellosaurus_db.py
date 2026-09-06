"""Parse the Cellosaurus flat file into a compact cell-line name vocabulary
(cellosaurus.sqlite) for deterministic cell-line recognition in Phase 2.

Input : $CELLOSAURUS_TXT (default ./cellosaurus.txt — ExPASy flat file)
Output: $CELLLINE_DB     (default ./cellosaurus.sqlite)

A reference vocabulary (like mesh.sqlite), NOT a hardcoded example list:
recognized names come from Cellosaurus, not from any scanned GEO sample.

Schema:
  cell_lines(name TEXT COLLATE NOCASE, cvcl TEXT, primary_name TEXT)
             name = the recommended ID OR any synonym (one row each)
  index on name (NOCASE) for O(1) exact lookup.
"""

import os, re, sqlite3, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TXT = os.environ.get("CELLOSAURUS_TXT", str(HERE / "cellosaurus.txt"))
DB = os.environ.get("CELLLINE_DB", str(HERE / "cellosaurus.sqlite"))


def main():
    t0 = time.time()
    con = sqlite3.connect(DB)
    con.executescript(
        "DROP TABLE IF EXISTS cell_lines;"
        "CREATE TABLE cell_lines (name TEXT NOT NULL COLLATE NOCASE, cvcl TEXT, primary_name TEXT);"
    )
    rows = []
    cvcl = pid = None
    names = []
    n = 0
    with open(TXT, encoding="utf-8", errors="ignore") as f:
        for line in f:
            tag = line[:2]
            if tag == "ID":
                pid = line[5:].strip()
                names = [pid]
            elif tag == "AC":
                cvcl = line[5:].strip()
            elif tag == "SY":
                syn = line[5:].strip()
                if syn:
                    names += [s.strip() for s in syn.split(";") if s.strip()]
            elif line.startswith("//"):
                for nm in set(names):
                    if nm:
                        rows.append((nm, cvcl, pid))
                n += 1
                cvcl = pid = None
                names = []
                if len(rows) >= 20000:
                    con.executemany("INSERT INTO cell_lines VALUES (?,?,?)", rows)
                    rows = []
    if rows:
        con.executemany("INSERT INTO cell_lines VALUES (?,?,?)", rows)
    con.execute("CREATE INDEX idx_cl_name ON cell_lines(name COLLATE NOCASE)")
    con.commit()
    cnt = con.execute("SELECT COUNT(*) FROM cell_lines").fetchone()[0]
    con.close()
    print(f"cell lines: {n}  names: {cnt}  ({time.time()-t0 :.1f}s) -> {DB}")


if __name__ == "__main__":
    main()
