"""Embed the normalization vocabulary for candidate retrieval.

One vector per DISTINCT canonical surface form, mapped back to its identifier,
so the index stays close to the size of the concept space rather than the term
space. Cell lines are embedded alongside MeSH terms and tagged so retrieval can
distinguish them.

Vocabulary strings pass through the same canonicalizer as corpus values: a rule
applied to one side only silently loses every pair that differs by that rule.
For the same reason the index must be rebuilt whenever the canonicalizer
changes, since forms embedded under the old rules no longer correspond to the
queries produced under the new ones.

Usage:
    python build_index.py [vocab_sqlite] [cellosaurus_sqlite] [out_npz]
"""

import os
import sqlite3
import sys
import time

import numpy as np
from sentence_transformers import SentenceTransformer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from phase2_normalize import canonicalize

VOCAB = sys.argv[1] if len(sys.argv) > 1 else "Directory to vocab.sqlite file"
CELLS = sys.argv[2] if len(sys.argv) > 2 else "Directory to cellosaurus.sqlite file"
OUT = sys.argv[3] if len(sys.argv) > 3 else "Directory to vocab index file"
MODEL = os.environ.get("PHASE2_EMBED_MODEL", "FremyCompany/BioLORD-2023")
DEVICE = os.environ.get("PHASE2_EMBED_DEVICE", "cuda")
BATCH = int(os.environ.get("PHASE2_EMBED_BATCH", "1024"))

print("collecting vocabulary surface forms ...", flush=True)


class FormTable:
    """Canonical-form -> entry map that refuses to guess between homonyms.

    The defect this replaces was structural, not a bad row: the loader used
    ``if c not in forms``, so when several DIFFERENT concepts reduced to one
    canonical form the FIRST one encountered silently won and the rest became
    invisible. Nothing recorded that a choice had been made, so a retrieval hit
    on that form looked exactly as confident as an unambiguous one -- which is
    how a three-letter cell-line code came to name a fish, a sheep or a mouse
    line in a corpus of human samples.

    The rule here is about the SHAPE of the evidence, not about any particular
    name: a surface form that maps to more than one distinct identifier carries
    no information about WHICH concept was meant, so it is not a usable
    retrieval key at all. Such forms are withheld from the index and fall
    through to the context-aware picker, which is the stage that can actually
    disambiguate. Adding a vocabulary, or a new release of an existing one,
    cannot reintroduce the defect: collisions are detected from the data every
    time the index is built, never enumerated in code.
    """

    def __init__(self):
        self._entry = {}          # canonical -> payload of the first source to claim it
        self._ids = {}            # canonical -> set of distinct ids seen
        self._source = {}         # canonical -> vocabulary that claimed it

    def add(self, canonical, ident, payload, source):
        if not canonical or not ident:
            return
        seen = self._ids.setdefault(canonical, set())
        seen.add(ident)
        # First vocabulary to claim a form keeps precedence (MeSH before
        # Cellosaurus, as before); later vocabularies only fill genuine gaps.
        if canonical not in self._entry:
            self._entry[canonical] = payload
            self._source[canonical] = source

    def unambiguous(self):
        """Forms backed by exactly one identifier, plus the collision report."""
        good, dropped = {}, {}
        for c, ids in self._ids.items():
            if len(ids) == 1:
                good[c] = self._entry[c]
            else:
                dropped[c] = (self._source[c], sorted(ids))
        return good, dropped


table = FormTable()

con = sqlite3.connect(f"file:{VOCAB}?mode=ro", uri=True)
n_mesh = 0
for term, mid, pref, kind, cat in con.execute(
    "SELECT term, mesh_id, preferred, kind, category FROM vocab"
):
    if not term:
        continue
    c = canonicalize(term)
    if c:
        table.add(c, mid, (mid, pref, cat or "", kind), "mesh")
        n_mesh += 1
con.close()
print("  mesh terms read: %d" % n_mesh, flush=True)

if os.path.exists(CELLS):
    cl = sqlite3.connect(f"file:{CELLS}?mode=ro", uri=True)
    # organism travels with the entry so a species contradiction is checkable
    # downstream at all; the released index carried no organism, which is why
    # the species screen could only ever run offline, after the fact.
    has_organism = any(
        r[1] == "organism"
        for r in cl.execute("PRAGMA table_info(cell_lines)")
    )
    cols = "name, cvcl, primary_name" + (", organism" if has_organism else "")
    for row in cl.execute("SELECT %s FROM cell_lines" % cols):
        name, cvcl, primary = row[0], row[1], row[2]
        organism = row[3] if has_organism else ""
        for n in (name, primary):
            if not n:
                continue
            c = canonicalize(n)
            if c:
                table.add(
                    c, cvcl, (cvcl, primary or name, organism or "", "cellosaurus"),
                    "cellosaurus",
                )
    cl.close()
    if not has_organism:
        print(
            "  *** WARNING: cellosaurus.sqlite has no organism column -> species "
            "verification cannot run. Rebuild it with build_cellosaurus_db.py. ***",
            flush=True,
        )
else:
    print("  cellosaurus source missing at %s; skipped" % CELLS, flush=True)

forms, collisions = table.unambiguous()
keys = sorted(forms)
print("  distinct canonical forms: %d" % len(keys), flush=True)
print(
    "  withheld as ambiguous (>1 identifier per surface form): %d"
    % len(collisions),
    flush=True,
)
if collisions:
    # A silent drop is the same failure as a silent pick, so the collisions are
    # written out rather than merely counted.
    report = (os.path.splitext(OUT)[0] or OUT) + ".collisions.tsv"
    os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
    with open(report, "w", encoding="utf-8") as fh:
        fh.write("canonical_form\tclaimed_by\tcompeting_ids\n")
        for c in sorted(collisions):
            src, ids = collisions[c]
            fh.write("%s\t%s\t%s\n" % (c, src, ";".join(ids)))
    print("  collision report: %s" % report, flush=True)

model = SentenceTransformer(MODEL, device=DEVICE)
model.max_seq_length = 64

t0 = time.time()
vecs = model.encode(
    keys,
    batch_size=BATCH,
    convert_to_numpy=True,
    normalize_embeddings=True,
    show_progress_bar=False,
)
print(
    "  encoded %d in %.0fs (%.0f/s)"
    % (len(keys), time.time() - t0, len(keys) / max(time.time() - t0, 1e-6)),
    flush=True,
)

os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
tmp = OUT + ".tmp.npz"
np.savez(
    tmp,
    vectors=vecs.astype(np.float32),
    forms=np.array(keys, dtype=object),
    ids=np.array([forms[k][0] for k in keys], dtype=object),
    names=np.array([forms[k][1] for k in keys], dtype=object),
    categories=np.array([forms[k][2] for k in keys], dtype=object),
    kinds=np.array([forms[k][3] for k in keys], dtype=object),
)
# NOTE: for cellosaurus entries the `categories` slot now carries the organism
# string (empty for MeSH terms), so a consumer can reject a non-human line
# without a second database round-trip.
os.replace(tmp, OUT)
print("wrote %s  shape=%s" % (OUT, vecs.shape), flush=True)
print("INDEX_DONE")
