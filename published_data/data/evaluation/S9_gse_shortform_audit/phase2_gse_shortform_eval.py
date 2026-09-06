"""Accuracy of Phase-2 GSE-context short-form disambiguation (deterministic).

Phase 2 resolves ambiguous short forms (AD, CRC, MS, ...) PER STUDY using GSE
context, then re-normalizes the expansion. This audits every such expansion by
checking it is GROUNDED in that study's own GSE text (title+summary+design):
an expansion "AD -> Alzheimer Disease in GSE X" is correct iff GSE X's text
actually contains the concept (matched via the MeSH descriptor's names/synonyms,
so surface-form reordering like COPD->'Pulmonary Disease, Chronic Obstructive'
still matches). 100% coverage of the expansion events, no LLM.
"""

import gzip, json, re, sqlite3, collections

DICT = "Directory to Phase 2 dictionary.json.gz"
GSEM = "Directory to gse_meta_scraped.json.gz"
MESH = "Directory to reference mesh.sqlite"
OUT = "Directory to output"


def canon(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def is_short(r):
    r = r.strip()
    return (
        2 <= len(r) <= 6
        and re.match(r"^[A-Za-z0-9/+\-\.]+$", r)
        and sum(c.isupper() for c in r) >= 2
    )


con = sqlite3.connect(MESH)
mesh_syn = collections.defaultdict(set)
for mid, name in con.execute("SELECT id,name FROM mesh_terms"):
    if len(canon(name)) >= 4:
        mesh_syn[mid].add(canon(name))
for mid, syn in con.execute("SELECT mesh_id,synonym FROM mesh_synonyms"):
    if len(canon(syn)) >= 4:
        mesh_syn[mid].add(canon(syn))

gse_meta = json.load(gzip.open(GSEM, "rt"))
gse_text = {
    g: " "
    + canon(
        " ".join(
            [m.get("gse_title", ""), m.get("gse_summary", ""), m.get("gse_design", "")]
        )
    )
    + " "
    for g, m in gse_meta.items()
}

STOP = {
    "disease",
    "diseases",
    "disorder",
    "disorders",
    "syndrome",
    "syndromes",
    "the",
    "of",
    "and",
    "cell",
    "cells",
    "carcinoma",
    "cancer",
    "tumor",
    "tumour",
    "chronic",
    "acute",
    "human",
    "primary",
    "type",
    "non",
    "infection",
    "infections",
}


def content_tokens(s):
    return {w for w in canon(s).split() if len(w) > 3 and w not in STOP}


d = json.load(gzip.open(DICT, "rt"))
events = []
val_inst = collections.Counter()
for F in ("Tissue", "Condition", "Treatment"):
    for raw, v in d[F].items():
        if "by_gse" not in v or not is_short(raw):
            continue
        val_inst[F] += v["count"]
        n_gse = max(len(v["by_gse"]), 1)
        share = v["count"] / n_gse
        for gse, rv in v["by_gse"].items():
            t = rv.get("target", "") or ""
            idv = rv.get("id", "") or ""
            if canon(t) == canon(raw) or len(canon(t)) <= len(canon(raw)) + 2:
                continue
            txt = gse_text.get(gse, "")
            acr_in = bool(txt) and (" " + canon(raw) + " ") in txt
            grounded = False
            how = "unsupported"
            idprefix = idv.split(";")[0].strip()
            if txt:
                if idprefix.startswith("CVCL"):

                    if acr_in:
                        grounded, how = True, "cellline_acronym_in_gse"
                else:
                    forms = mesh_syn.get(idprefix, set()) or {canon(t)}
                    ct = content_tokens(t)
                    if any(f in txt for f in forms):
                        grounded, how = True, "expansion_in_gse"
                    elif ct and sum(1 for w in ct if w in txt) >= max(1, len(ct) * 0.6):
                        grounded, how = True, "expansion_tokens_in_gse"

            weak = (not grounded) and acr_in
            events.append((F, raw, gse, t, idv, share, grounded, weak, how))

tot = len(events)
gr = sum(1 for e in events if e[6])
wk = sum(1 for e in events if e[7])
print(f"GSE-context short-form EXPANSIONS audited: {tot :,}")
print(
    f"  VERIFIED (expanded concept present in the study's own GSE text): {gr :,} ({100 * gr / tot :.1f}%)"
)
print(
    f"  context-present (acronym attested, expansion is standard meaning): +{wk :,} ({100 * wk / tot :.1f}%)"
)
print(
    f"  => attested in study either way: {gr + wk :,} ({100 *(gr + wk)/tot :.1f}%);  unattested {tot - gr - wk :,} ({100 *(tot - gr - wk)/tot :.1f}%)"
)
byF = collections.defaultdict(lambda: [0, 0, 0])
for e in events:
    byF[e[0]][0] += 1
    byF[e[0]][1] += int(e[6])
    byF[e[0]][2] += int(e[7])
for F in ("Tissue", "Condition", "Treatment"):
    t, g, wkf = byF[F]
    if t:
        print(
            f"    {F :10} verified {g :4}/{t :4} ({100 * g / t :.0f}%)  +acronym-attested {wkf :4}  (samples touched: {val_inst[F]:,})"
        )
inst_tot = sum(e[5] for e in events)
inst_gr = sum(e[5] for e in events if e[6] or e[7])
print(
    f"  instance-weighted attested (verified+context): {inst_gr :,.0f}/{inst_tot :,.0f} ({100 * inst_gr / max(inst_tot ,1):.1f}%)"
)

print(
    "\n=== examples: VERIFIED disease-acronym expansions (acronym -> MeSH, matched in GSE) ==="
)
seen = set()
for e in events:
    F, raw, gse, t, idv, sh, g, wkk, how = e
    if (
        F == "Condition"
        and g
        and re.match(r"[DC]\d", idv)
        and (raw, canon(t)) not in seen
    ):
        seen.add((raw, canon(t)))
        title = gse_meta.get(gse, {}).get("gse_title", "")[:70]
        print(f"  {raw :6} -> {t :40.40} [{gse}] {title!r}")
    if len(seen) >= 22:
        break

print(
    "\n=== UNATTESTED expansions (neither expansion nor acronym in GSE text — review) ==="
)
ung = [e for e in events if not e[6] and not e[7]]
for e in ung[:15]:
    F, raw, gse, t, idv, sh, g, wkk, how = e
    title = gse_meta.get(gse, {}).get("gse_title", "")[:58]
    print(f"  [{F}] {raw :6} -> {t :32.32} [{gse}] {title!r}")

sense = collections.defaultdict(set)
for e in events:
    sense[(e[0], e[1])].add(canon(e[3]))
print("\n=== genuinely AMBIGUOUS acronyms (>=2 distinct expansions across studies) ===")
for (F, raw), s in sorted(sense.items(), key=lambda kv: -len(kv[1])):
    if len(s) >= 2:
        print(f"  [{F}] {raw}: " + " | ".join(sorted(s)[:5]))

import csv

with open(f"{OUT}/E_gse_shortform_audit.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(
        [
            "field",
            "acronym",
            "gse",
            "expansion",
            "id",
            "verified",
            "acronym_attested",
            "how",
        ]
    )
    for e in events:
        F, raw, gse, t, idv, sh, g, wkk, how = e
        w.writerow([F, raw, gse, t, idv, g, wkk, how])
print(f"\nwrote E_gse_shortform_audit.csv ({tot} events) to", OUT)
