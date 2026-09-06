#!/usr/bin/env python3
"""Build geo_metadata.sqlite from the shipped samples_804k.json.

Several evaluation scripts read the raw sample metadata through a SQLite
database (`select gsm, title, source_name, characteristics, treatment_protocol,
description from sample`): the Sex/Age grounding audit of Table S19, the Age
provenance of Table S20, the gold-source verification in S1, and the Figure S1
generator. The database is a convenience index over samples_804k.json, which is
the file this package actually ships, so it is built here rather than shipped
twice; the JSON is the record of what GEO returned and the database holds
nothing that is not in it.

Run once before those scripts:

    python3 build_geo_metadata.py samples_804k.json geo_metadata.sqlite
"""

import json
import os
import sqlite3
import sys

FIELDS = ("title", "source_name", "characteristics", "treatment_protocol",
          "description")


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "samples_804k.json"
    out = sys.argv[2] if len(sys.argv) > 2 else "geo_metadata.sqlite"
    if os.path.exists(out):
        os.remove(out)

    with open(src) as fh:
        samples = json.load(fh)

    con = sqlite3.connect(out)
    con.execute("CREATE TABLE sample (gsm TEXT PRIMARY KEY, %s)"
                % ", ".join("%s TEXT" % f for f in FIELDS))
    con.executemany(
        "INSERT OR REPLACE INTO sample VALUES (?,?,?,?,?,?)",
        ((s.get("gsm"),) + tuple(s.get(f) or "" for f in FIELDS)
         for s in samples))
    con.commit()
    n = con.execute("SELECT count(*) FROM sample").fetchone()[0]
    con.close()
    print("wrote %s with %d samples" % (out, n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
