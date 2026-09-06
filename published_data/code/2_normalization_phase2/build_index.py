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
con = sqlite3.connect(f"file:{VOCAB}?mode=ro", uri=True)
forms = {}
for term, mid, pref, kind, cat in con.execute(
    "SELECT term, mesh_id, preferred, kind, category FROM vocab"
):
    if not term:
        continue
    c = canonicalize(term)
    if c and c not in forms:
        forms[c] = (mid, pref, cat or "", kind)
con.close()
print("  mesh forms: %d" % len(forms), flush=True)

if os.path.exists(CELLS):
    cl = sqlite3.connect(f"file:{CELLS}?mode=ro", uri=True)
    for name, cvcl, primary in cl.execute(
        "SELECT name, cvcl, primary_name FROM cell_lines"
    ):
        for n in (name, primary):
            if not n:
                continue
            c = canonicalize(n)
            if c and c not in forms:
                forms[c] = (cvcl, primary or name, "CELLLINE", "cellosaurus")
    cl.close()
else:
    print("  cellosaurus source missing at %s; skipped" % CELLS, flush=True)

keys = sorted(forms)
print("  distinct canonical forms: %d" % len(keys), flush=True)

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
os.replace(tmp, OUT)
print("wrote %s  shape=%s" % (OUT, vecs.shape), flush=True)
print("INDEX_DONE")
