"""Build the normalization vocabulary from MeSH.

Four kinds of surface form are collected, because a corpus label may match any
of them:

  descriptor  preferred term of a MeSH descriptor
  entry       descriptor synonym ("Vitamin C" for "Ascorbic Acid")
  scr         supplementary concept record, where most drugs and many rare
              diseases are defined
  scr_entry   synonym of a supplementary concept

Supplementary records are essential rather than optional: descriptors alone
cover a small fraction of the Treatment vocabulary, and without them drug names
fall through to out-of-vocabulary.

Each row carries a tree category, used downstream to restrict candidates to
plausible branches per field (anatomy for Tissue, disease for Condition,
chemicals for Treatment). Supplementary records carry no tree number and are
marked SCR so they stay eligible for every field.

Usage:
    python build_vocab.py [xml_dir] [out_sqlite]
"""

import os
import re
import sqlite3
import sys
import time
from xml.etree import ElementTree as ET

XML_DIR = sys.argv[1] if len(sys.argv) > 1 else "Directory to MeSH build dir"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(XML_DIR, "vocab.sqlite")
DESC = os.path.join(XML_DIR, "desc2026.xml")
SUPP = os.path.join(XML_DIR, "supp2026.xml")

_nonword = re.compile(r"[^a-z0-9 ]+")
_ws = re.compile(r"\s+")


def norm(s):
    return _ws.sub(" ", _nonword.sub(" ", str(s).lower())).strip()


rows = []


def add(term, mid, pref, kind, cat):
    t = (term or "").strip()
    if t:
        rows.append((t, norm(t), mid, pref, kind, cat))


def on_descriptor(el):
    mid = el.findtext("DescriptorUI") or ""
    pref = el.findtext("DescriptorName/String") or ""
    cats = set()
    for tn in el.findall("TreeNumberList/TreeNumber"):
        if tn.text:
            cats.add(tn.text[0])
    cat = "".join(sorted(cats))
    add(pref, mid, pref, "descriptor", cat)
    for term in el.findall(".//Concept//Term/String"):
        add(term.text, mid, pref, "entry", cat)


def on_supplemental(el):
    mid = el.findtext("SupplementalRecordUI") or ""
    pref = el.findtext("SupplementalRecordName/String") or ""
    add(pref, mid, pref, "scr", "SCR")
    for term in el.findall(".//Concept//Term/String"):
        add(term.text, mid, pref, "scr_entry", "SCR")


def stream(path, tag, handler, label):
    """Parse incrementally and clear each record; the sources are ~1 GB."""
    if not os.path.exists(path):
        raise SystemExit("missing %s" % path)
    t0 = time.time()
    n = 0
    for _ev, el in ET.iterparse(path, events=("end",)):
        if el.tag != tag:
            continue
        handler(el)
        n += 1
        el.clear()
        if n % 25000 == 0:
            print(
                "  %s: %d (%.0f/s)" % (label, n, n / max(time.time() - t0, 1e-6)),
                flush=True,
            )
    print("  %s: %d total in %.0fs" % (label, n, time.time() - t0), flush=True)


def main():
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    db = sqlite3.connect(OUT)
    db.executescript(
        "PRAGMA journal_mode=OFF;"
        "PRAGMA synchronous=OFF;"
        "DROP TABLE IF EXISTS vocab;"
        "CREATE TABLE vocab(term TEXT, term_norm TEXT, mesh_id TEXT,"
        " preferred TEXT, kind TEXT, category TEXT);"
    )

    print("parsing descriptors ...", flush=True)
    stream(DESC, "DescriptorRecord", on_descriptor, "desc")
    print("parsing supplementary concept records ...", flush=True)
    stream(SUPP, "SupplementalRecord", on_supplemental, "supp")

    db.executemany("INSERT INTO vocab VALUES (?,?,?,?,?,?)", rows)
    db.execute("CREATE INDEX ix_norm ON vocab(term_norm)")
    db.execute("CREATE INDEX ix_id ON vocab(mesh_id)")
    db.commit()

    for kind in ("descriptor", "entry", "scr", "scr_entry"):
        n = db.execute("SELECT COUNT(*) FROM vocab WHERE kind=?", (kind,)).fetchone()[0]
        print("  %-11s %9d" % (kind, n))
    total = db.execute("SELECT COUNT(*) FROM vocab").fetchone()[0]
    distinct = db.execute("SELECT COUNT(DISTINCT term_norm) FROM vocab").fetchone()[0]
    print("  TOTAL %d rows, %d distinct normalized terms" % (total, distinct))
    db.close()

    if total < 1200000:
        raise SystemExit("vocabulary incomplete (%d rows); expected ~1.35M" % total)
    print("VOCAB_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
