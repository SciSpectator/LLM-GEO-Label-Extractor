#!/usr/bin/env python3
"""Rebuild vocab.sqlite from mesh.sqlite when the raw MeSH XML is not at hand.

build_vocab.py reads the MeSH XML release. The shipped package carries the
already-parsed mesh.sqlite but not that XML, so this produces the same
vocabulary table from the database instead: every descriptor, every entry
synonym, and the tree category each one belongs to. The output schema is the
one build_index.py reads --

    vocab(term, mesh_id, preferred, kind, category)

so the two are interchangeable as far as the index build is concerned.

kind      'descriptor' for a preferred term, 'entry' for a synonym.
category  first letter of the MeSH tree number (A anatomy, C disease,
          D chemical, ...), used downstream to keep candidates in a plausible
          branch per field. Records with no tree number are marked SCR, which
          leaves them eligible for every field -- the same treatment
          build_vocab.py gives supplementary concepts.

Usage:
    python3 build_vocab_from_meshdb.py mesh.sqlite vocab.sqlite
"""

import sqlite3
import sys
import time


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "mesh.sqlite"
    out = sys.argv[2] if len(sys.argv) > 2 else "vocab.sqlite"

    t0 = time.time()
    mesh = sqlite3.connect(f"file:{src}?mode=ro", uri=True)

    # one category per concept: the shallowest tree number wins, so a concept
    # indexed in several branches is filed under its primary one
    cat = {}
    for mid, tree in mesh.execute("SELECT mesh_id, tree_number FROM mesh_tree"):
        if not tree:
            continue
        depth = tree.count(".")
        cur = cat.get(mid)
        if cur is None or depth < cur[0]:
            cat[mid] = (depth, tree[0])

    con = sqlite3.connect(out)
    con.executescript(
        "DROP TABLE IF EXISTS vocab;"
        "CREATE TABLE vocab (term TEXT NOT NULL COLLATE NOCASE, mesh_id TEXT NOT NULL,"
        " preferred TEXT, kind TEXT, category TEXT);"
    )

    preferred = {}
    rows = []
    n_desc = n_entry = 0
    for mid, name in mesh.execute("SELECT id, name FROM mesh_terms"):
        if not name:
            continue
        preferred[mid] = name
        rows.append((name, mid, name, "descriptor", cat.get(mid, (0, "SCR"))[1]))
        n_desc += 1

    for mid, syn in mesh.execute("SELECT mesh_id, synonym FROM mesh_synonyms"):
        if not syn:
            continue
        rows.append((syn, mid, preferred.get(mid, syn), "entry",
                     cat.get(mid, (0, "SCR"))[1]))
        n_entry += 1

    con.executemany("INSERT INTO vocab VALUES (?,?,?,?,?)", rows)
    con.execute("CREATE INDEX idx_vocab_term ON vocab(term COLLATE NOCASE)")
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM vocab").fetchone()[0]
    con.close()

    print(f"vocab rows: {total:,}  (descriptors {n_desc:,}, entries {n_entry:,}) "
          f"in {time.time()-t0:.1f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
