#!/usr/bin/env python3
"""Phase 2: normalisation of Phase 1 labels against a controlled vocabulary.

This is the single driver for Phase 2. The domain logic lives unchanged in
phase2_mesh, phase2_llm, mesh_lookup, cellline_db, phase2_normalize and
phase2_curate; this file connects them and decides only what runs concurrently.

It replaces two partial drivers. One carried the study-context and warm-up
paths but never minted identifiers for values outside the vocabulary; the other
minted them but resolved every sample serially. Neither produced a complete
corpus on its own.

Resolution order is Phase2Mesh's, not this file's: episodic memory, the
Cellosaurus reference gate for Tissue, the cell-line identity gate, exact MeSH,
existing out-of-vocabulary entries, candidate retrieval with the picker, and
minting. Two tiers stay off by default because enabling them was measured to
damage the corpus: the polarity tier erased about 66,000 already extracted
values to Not Specified, and the picker with study context injected reread
sample-level values against study-level text. Both remain available through
PHASE2_USE_POLARITY and PHASE2_USE_PICKER.

Concurrency follows data dependence. Every distinct pair of raw value and
column is resolved once, and those resolutions are independent, so they run at
the full configured width. Consolidation of out-of-vocabulary values is
sequential within a connected component of the similarity graph, because a
later spelling must be able to join a concept an earlier one created, and
independent across components, because values in different components are
further apart than the merge threshold and can never be candidates for one
another.

Hardware is discovered, not assumed. Endpoints are probed at startup and work
is spread over whatever responds.
"""

from __future__ import annotations

import argparse
import csv
import glob
import gzip
import hashlib
import json
import os
import platform
import sys
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase2_mesh import LABEL_COLS, NS, Phase2Mesh, _is_shortform
from mesh_lookup import MeshDB

try:
    from gse_context_cache import GSEContextCache
except Exception:
    GSEContextCache = None

CELL_LINE_COL = "Tissue"

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(f"[phase2] {msg}", flush=True)


def canonical(raw: str) -> str:
    """Surface form used to recognise one spelling written several ways.

    Punctuation is separator, not content: DMSO_Vehicle, DMSO-vehicle and
    DMSO vehicle are one spelling, and treating them as three fragments a
    concept before any model sees it. Nothing here decides meaning.
    """
    s = str(raw or "").lower()
    return " ".join(
        "".join(c if (c.isalnum() or c.isspace()) else " " for c in s).split()
    )


def blocking_key(raw: str) -> str:
    """Order-independent form, so word-order variants reach the model together.

    Vehicle control and Control vehicle are the same words in a different
    order. This only ensures such pairs become candidates for one another;
    whether they name one concept remains the model's decision.
    """
    return " ".join(sorted(canonical(raw).split()))


def file_digest(path: str, limit: int = 1 << 24) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read(limit))
    return h.hexdigest()[:16]


def discover_endpoints(spec: str, host: str, first: int, probe: int) -> list[str]:
    if spec.strip():
        urls = [u.strip().rstrip("/") for u in spec.split(",") if u.strip()]
    else:
        urls = [f"http://{host}:{first +i}/v1" for i in range(probe)]
    alive = []
    for u in urls:
        try:
            with urllib.request.urlopen(u + "/models", timeout=4):
                alive.append(u)
        except Exception:
            continue
    return alive


def load_corpus(paths: list[str], stage: str):
    """Distinct values with counts, and the study each was first seen in."""
    freq = {c: Counter() for c in LABEL_COLS}
    gse_of = {c: {} for c in LABEL_COLS}
    gses_of = {c: {} for c in LABEL_COLS}
    n = 0
    for p in paths:
        op = gzip.open if p.endswith(".gz") else open
        with op(p, "rt") as fh:
            d = json.load(fh)
        rows = d["samples"] if isinstance(d, dict) and "samples" in d else d
        for s in rows:
            n += 1
            src = s.get(stage) or {}
            gse = s.get("gse") or ""
            for c in LABEL_COLS:
                v = str(src.get(c, "") or "").strip()
                if v:
                    freq[c][v] += 1
                    gse_of[c].setdefault(v, gse)
                    if gse and _is_shortform(v):
                        gses_of[c].setdefault(v, set()).add(gse)
    return freq, gse_of, gses_of, n


def detect_stage(paths: list[str], requested: str) -> str:
    if requested:
        return requested
    op = gzip.open if paths[0].endswith(".gz") else open
    with op(paths[0], "rt") as fh:
        d = json.load(fh)
    rows = d["samples"] if isinstance(d, dict) and "samples" in d else d
    for candidate in ("phase1b", "phase1"):
        for s in rows[:2000]:
            src = s.get(candidate) or {}
            if any(
                str(src.get(c, "") or "").strip() not in ("", NS) for c in LABEL_COLS
            ):
                return candidate
    raise SystemExit("no stage with populated labels found; pass --stage")


def load_context(path: str) -> dict[str, str]:
    if not path or not os.path.exists(path):
        return {}
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt") as fh:
        meta = json.load(fh)
    out = {}
    for gse, m in meta.items():
        ctx = "\n".join(
            [
                (m.get("gse_title") or m.get("title") or ""),
                (m.get("gse_summary") or m.get("summary") or ""),
                (m.get("gse_design") or m.get("overall_design") or ""),
            ]
        ).strip()
        if ctx:
            out[gse] = ctx
    return out


def resolve_all(p2: Phase2Mesh, freq, gse_of, gses_of, ctx_map, workers: int):
    """Resolve every distinct pair concurrently.

    Ambiguous short-forms (bare acronyms like 'AD', 'CRC') are resolved PER GSE,
    because their expansion depends on the study; the per-study results are kept
    under res[col][raw]['by_gse'][gse]. Every other value is resolved once.

    A cell line answers Tissue only; a CVCL id landing outside Tissue is refused
    (blanked) and left for consolidation / field normalization. ART-*/OOV-* ids
    are classified 'oov' so consolidation merges them.
    """
    res = {c: {} for c in LABEL_COLS}
    jobs = []
    for c in LABEL_COLS:
        for raw in freq[c]:
            gses = gses_of[c].get(raw)
            if gses and _is_shortform(raw):
                for g in gses:
                    jobs.append((c, raw, g))
            else:
                jobs.append((c, raw, None))
    done = [0]
    refused = [0]
    lock = threading.Lock()
    total = len(jobs)

    def one(job):
        col, raw, gse = job
        g = gse if gse is not None else (gse_of[col].get(raw) or None)
        ctx = ctx_map.get(g, "") if g else ""
        try:
            out = p2.collapse(raw, col, context=ctx, gse_id=g)
            err = ""
        except Exception as e:
            out, err = {}, f"error:{e.__class__.__name__}"
        label = str(out.get("label") or raw).strip() or raw
        ident = str(out.get("id") or "").strip()
        comps = out.get("components") or []
        off_column_cell_line = ident.startswith("CVCL") and col != CELL_LINE_COL
        if off_column_cell_line:
            label, ident, comps = raw, "", []
        if err:
            source = err
        elif not ident:
            source = "uncovered"
        elif ident.startswith("CVCL"):
            source = "cellosaurus"
        elif ident.startswith(("OOV", "ART")):
            source = "oov"
        else:
            source = "mesh"
        rec = {"target": label, "id": ident, "source": source}
        with lock:
            if off_column_cell_line:
                refused[0] += 1
            if gse is not None:
                e = res[col].setdefault(
                    raw,
                    {
                        "target": NS,
                        "id": "",
                        "source": "uncovered",
                        "components": 0,
                        "count": freq[col][raw],
                        "by_gse": {},
                    },
                )
                e["by_gse"][gse] = rec
            else:
                res[col][raw] = {
                    **rec,
                    "components": len(comps),
                    "count": freq[col][raw],
                }
            done[0] += 1
            if done[0] % 5000 == 0:
                log(f"  resolved {done[0]:,}/{total:,}")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, jobs))

    n_sf = 0
    for c in LABEL_COLS:
        for raw, e in res[c].items():
            if e.get("by_gse"):
                n_sf += 1
                rep = gse_of[c].get(raw)
                pick = e["by_gse"].get(rep) or next(iter(e["by_gse"].values()))
                e["target"], e["id"], e["source"] = (
                    pick["target"],
                    pick["id"],
                    pick["source"],
                )
    log(
        f"resolved {total:,} jobs "
        f"({sum(len(freq[c]) for c in LABEL_COLS):,} distinct values, "
        f"{n_sf:,} ambiguous short-forms resolved per-study) "
        f"in {time.time()-t0:.0f}s"
    )
    if refused[0]:
        log(
            f"  refused {refused[0]:,} cell line identifiers outside "
            f"{CELL_LINE_COL}"
        )
    return res


def components(
    vectors: np.ndarray, threshold: float, order: list[int], block: int, keys: list[str]
):
    n = len(vectors)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_key = defaultdict(list)
    for i, k in enumerate(keys):
        if k:
            by_key[k].append(i)
    for grp in by_key.values():
        for other in grp[1:]:
            ra, rb = find(grp[0]), find(other)
            if ra != rb:
                parent[rb] = ra

    for i in range(0, n, block):
        sims = vectors[i : i + block] @ vectors.T
        for j in range(sims.shape[0]):
            gi = i + j
            for k in np.nonzero(sims[j] >= threshold)[0]:
                k = int(k)
                if k != gi:
                    ra, rb = find(gi), find(k)
                    if ra != rb:
                        parent[rb] = ra
    buckets = defaultdict(list)
    for i in order:
        buckets[find(i)].append(i)
    return sorted(buckets.values(), key=len, reverse=True)


def mint_id(name: str, col: str) -> str:
    prefix = {"Tissue": "T", "Condition": "C", "Treatment": "X"}.get(col, "X")
    key = f"{col}\x00{' '.join(str(name or '').lower().split())}"
    return f"OOV-{prefix}-{hashlib.sha1(key.encode()).hexdigest()[:10].upper()}"


def consolidate(
    res, p2: Phase2Mesh, embed, threshold: float, top_n: int, workers: int, block: int
):
    """Group values no vocabulary covered, so one concept gets one identifier."""
    import phase2_llm

    for col in LABEL_COLS:

        vals = [
            r
            for r, v in res[col].items()
            if v.get("source") in ("uncovered", "oov")
            and "by_gse" not in v
            and str(v.get("target") or "").strip()
            and str(v.get("target")).strip().lower() != NS.lower()
        ]
        if not vals:
            continue
        t0 = time.time()
        vecs = embed(vals)
        order = sorted(
            range(len(vals)),
            key=lambda i: (-(res[col][vals[i]].get("count") or 0), vals[i]),
        )
        comps = components(
            vecs, threshold, order, block, [blocking_key(v) for v in vals]
        )
        log(
            f"  {col}: {len(vals):,} uncovered values, {len(comps):,} "
            f"independent components, largest {len(comps[0]):,}"
        )
        parts, lock, tally = [], threading.Lock(), {"asked": 0, "merged": 0}

        def run(positions):
            names, cvecs, members, seen = [], [], [], {}
            asked = merged = 0
            for pos in positions:
                label = vals[pos]
                key = canonical(label)
                if key in seen:
                    members[seen[key]].append(label)
                    merged += 1
                    continue
                pick = None
                if names:
                    sims = np.asarray(cvecs, dtype=np.float32) @ vecs[pos]
                    near = sorted(
                        (
                            (float(sims[c]), c)
                            for c in range(len(names))
                            if float(sims[c]) >= threshold
                        ),
                        key=lambda t: -t[0],
                    )[:top_n]
                    if near:
                        asked += 1
                        try:
                            out = phase2_llm.assign_concept(
                                label, col, [names[c] for _s, c in near]
                            )
                            ci = out.get("concept")
                        except Exception:
                            ci = None
                        if isinstance(ci, int) and 0 <= ci < len(near):
                            pick = near[ci][1]
                if pick is None:
                    seen[key] = len(names)
                    names.append(label)
                    cvecs.append(vecs[pos])
                    members.append([label])
                else:
                    members[pick].append(label)
                    merged += 1
            with lock:
                tally["asked"] += asked
                tally["merged"] += merged
                parts.append((names, members))

        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(run, comps))
        concepts = 0
        for names, members in parts:
            for k, group in enumerate(members):
                ident = mint_id(names[k], col)
                for raw in group:
                    res[col][raw].update(
                        {"target": names[k], "id": ident, "source": "oov"}
                    )
                concepts += 1
        log(
            f"  {col}: {tally['asked']:,} model calls, {tally['merged']:,} "
            f"folded, {concepts:,} concepts ({time.time()-t0:.0f}s)"
        )


def field_reroutes(res, p2):
    """Field-appropriateness normalization (cross-field). A field must hold only
    its own kind of concept; a value sitting in the wrong field is filtered out
    and, when unambiguous, re-routed to the field it belongs to:

      * a DISEASE in Tissue  -> Condition   (e.g. 'colorectal cancer' as a Tissue)
      * a CELL LINE in Condition/Treatment  -> Tissue

    Detection is a factual controlled-vocabulary check (not a fuzzy merge): a
    Tissue value that matches a MeSH disease (C/F branch) but NO anatomy (A) is a
    misplaced disease; a Condition/Treatment value that is a catalogued
    Cellosaurus line is a misplaced cell line.
    """
    try:
        from cellline_db import CellLineDB

        cldb = CellLineDB.get()
    except Exception:
        cldb = None
    disease_in_tissue = {}
    cellline_in_field = {"Condition": {}, "Treatment": {}}

    for raw, v in res.get("Tissue", {}).items():
        if v.get("source") not in ("oov", "uncovered"):
            continue
        cond = p2.db.lookup_mesh(raw, "Condition")
        if len(cond) == 1 and not p2.db.lookup_mesh(raw, "Tissue"):
            disease_in_tissue[raw] = (cond[0]["name"], cond[0]["id"])

    if cldb is not None:
        for col in ("Condition", "Treatment"):
            for raw in res.get(col, {}):
                ref = cldb.match_ref(raw)
                if not (ref and ref[0]):
                    continue
                try:
                    confirmed = bool(p2._cellline_id(raw))
                except Exception:
                    confirmed = False
                if confirmed:
                    cellline_in_field[col][raw] = (ref[0], ref[1])
    log(
        f"field normalization: {len(disease_in_tissue):,} diseases misfiled as "
        f"Tissue -> Condition; "
        f"{sum(len(m) for m in cellline_in_field.values()):,} cell lines "
        f"misfiled as Condition/Treatment -> Tissue"
    )
    return disease_in_tissue, cellline_in_field


def export(
    paths: list[str],
    res,
    stage: str,
    out_dir: str,
    disease_in_tissue=None,
    cellline_in_field=None,
):
    os.makedirs(out_dir, exist_ok=True)
    header = ["gsm", "gse", "gpl"]
    for c in LABEL_COLS:
        header += [
            f"{stage}_{c}",
            f"final_{c}",
            f"final_{c}_id",
            f"final_{c}_source",
        ]
    writers, handles, n = {}, {}, 0
    for p in paths:
        op = gzip.open if p.endswith(".gz") else open
        with op(p, "rt") as fh:
            d = json.load(fh)
        rows = d["samples"] if isinstance(d, dict) and "samples" in d else d
        for s in rows:
            gpl = s.get("gpl") or "unknown"
            if gpl not in writers:
                os.makedirs(os.path.join(out_dir, gpl), exist_ok=True)
                h = gzip.open(
                    os.path.join(out_dir, gpl, f"{gpl}.csv.gz"), "wt", newline=""
                )
                w = csv.writer(h)
                w.writerow(header)
                writers[gpl], handles[gpl] = w, h
            src = s.get(stage) or {}
            gse_s = s.get("gse") or ""
            raws, cell = {}, {}
            for c in LABEL_COLS:
                raw = str(src.get(c, "") or "").strip()
                r = res[c].get(raw) or {}
                if r.get("by_gse"):
                    r = r["by_gse"].get(gse_s) or r
                raws[c] = raw
                cell[c] = [
                    r.get("target") or NS,
                    r.get("id") or "",
                    r.get("source") or "",
                ]

            def _empty(c):
                return cell[c][0] in (NS, "", None) or not cell[c][1]

            tr = raws["Tissue"]
            if disease_in_tissue and tr in disease_in_tissue:
                dname, did = disease_in_tissue[tr]
                cell["Tissue"] = [NS, "", "filtered:disease-in-tissue"]
                if _empty("Condition"):
                    cell["Condition"] = [dname, did, "rerouted:tissue->condition"]
            for c in ("Condition", "Treatment"):
                m = (cellline_in_field or {}).get(c, {})
                if raws[c] in m:
                    cvcl, primary = m[raws[c]]
                    cell[c] = [NS, "", f"filtered:cellline-in-{c.lower()}"]
                    if (
                        _empty("Tissue")
                        or cell["Tissue"][2] == "filtered:disease-in-tissue"
                    ):
                        cell["Tissue"] = [primary, cvcl, "rerouted:->tissue"]
            row = [s.get("gsm", ""), s.get("gse", ""), gpl]
            for c in LABEL_COLS:
                row += [raws[c], cell[c][0], cell[c][1], cell[c][2]]
            writers[gpl].writerow(row)
            n += 1
    for h in handles.values():
        h.close()
    log(f"exported {n:,} samples across {len(writers):,} platforms")
    return n, len(writers)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Normalise Phase 1 labels against a controlled vocabulary."
    )
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--stage", default="")
    ap.add_argument("--gse-meta", default="")
    ap.add_argument("--cache-db", default="gse_context_cache.sqlite")
    ap.add_argument("--index", required=True)
    ap.add_argument(
        "--embed-model", default="cambridgeltl/SapBERT-from-PubMedBERT-fulltext"
    )
    ap.add_argument("--embed-device", default="auto")
    ap.add_argument("--embed-batch", type=int, default=512)
    ap.add_argument("--urls", default="")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--first-port", type=int, default=8000)
    ap.add_argument("--probe-ports", type=int, default=16)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--top-n", type=int, default=8)
    ap.add_argument("--merge-threshold", type=float, default=0.95)
    ap.add_argument("--block", type=int, default=2048)
    ap.add_argument("--no-pubtator", action="store_true")
    ap.add_argument("--no-verifier", action="store_true")
    ap.add_argument("--out-dir", default="phase2_out")
    args = ap.parse_args()

    t0 = time.time()
    paths = sorted(glob.glob(args.corpus))
    if not paths:
        log(f"no corpus files matched {args.corpus !r}")
        return 2
    urls = discover_endpoints(args.urls, args.host, args.first_port, args.probe_ports)
    if not urls:
        log("no inference endpoint responded; pass --urls")
        return 2
    workers = args.workers or 32 * len(urls)
    os.environ.setdefault("PHASE2_VLLM_URLS", ",".join(urls))
    os.environ.setdefault("LLM_BACKEND", "vllm")
    os.environ["PHASE2_EMBED_MODEL"] = args.embed_model
    os.environ["BIOLORD_MODEL"] = args.embed_model
    log(f"corpus: {len(paths)} files")
    log(f"inference: {len(urls)} endpoints, {workers} concurrent")

    stage = detect_stage(paths, args.stage)
    freq, gse_of, gses_of, n_samples = load_corpus(paths, stage)
    n_dist = sum(len(freq[c]) for c in LABEL_COLS)
    log(f"stage {stage}: {n_dist:,} distinct values from {n_samples:,} samples")

    ctx_map = load_context(args.gse_meta)
    log(f"study context available for {len(ctx_map):,} studies")

    cache = None
    if GSEContextCache is not None:
        try:
            cache = GSEContextCache(args.cache_db)
        except Exception as e:
            log(f"context cache disabled: {e !r}")

    use_pol = os.environ.get("PHASE2_USE_POLARITY", "1") != "0"
    use_pick = os.environ.get("PHASE2_USE_PICKER", "1") != "0"
    log(
        f"tiers: polarity={'on'if use_pol else 'off'}, "
        f"picker={'on'if use_pick else 'off'}"
    )
    if not use_pick:
        log(
            "  *** WARNING: picker OFF -> exact-match only, no MeSH/Cellosaurus/"
            "OOV LLM normalization. Set PHASE2_USE_PICKER=1 for a real run. ***"
        )

    try:
        from cellline_db import CellLineDB, DB_PATH as _CL_DB

        if not CellLineDB.get()._exists:
            log(
                f"  *** WARNING: Cellosaurus DB not found at {_CL_DB} -> Tissue "
                "cell lines will NOT resolve to CVCL. Stage cellosaurus.sqlite "
                "or set CELLLINE_DB. ***"
            )
        else:
            log(f"  Cellosaurus reference active: {_CL_DB}")
    except Exception as _e:
        log(f"  *** WARNING: Cellosaurus lookup unavailable: {_e !r} ***")
    p2 = Phase2Mesh(
        db=MeshDB(),
        use_pubtator=not args.no_pubtator,
        use_verifier=not args.no_verifier,
        cache=cache,
        use_polarity=use_pol,
        use_picker=use_pick,
    )

    res = resolve_all(p2, freq, gse_of, gses_of, ctx_map, workers)

    from sentence_transformers import SentenceTransformer

    dev = args.embed_device
    if dev == "auto":
        try:
            import torch

            dev = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            dev = "cpu"
    st = SentenceTransformer(args.embed_model, device=dev)
    st.max_seq_length = 64

    def embed(values):
        return st.encode(
            values,
            batch_size=args.embed_batch,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).astype(np.float32)

    log(f"consolidation, embedder {args.embed_model} on {dev}")
    consolidate(res, p2, embed, args.merge_threshold, args.top_n, workers, args.block)

    disease_in_tissue, cellline_in_field = field_reroutes(res, p2)

    n_folded = casefold_oov(res)
    log(
        f"case-fold: unified {n_folded} out-of-vocabulary assignments across "
        f"case/whitespace/hyphen surface variants"
    )

    os.makedirs(args.out_dir, exist_ok=True)
    with gzip.open(os.path.join(args.out_dir, "dictionary.json.gz"), "wt") as fh:
        json.dump(res, fh)
    n_rows, n_plat = export(
        paths,
        res,
        stage,
        os.path.join(args.out_dir, "final"),
        disease_in_tissue,
        cellline_in_field,
    )

    sources = Counter()
    for c in LABEL_COLS:
        for r in res[c].values():
            sources[r.get("source") or "?"] += 1
    manifest = {
        "corpus": args.corpus,
        "corpus_files": len(paths),
        "stage": stage,
        "samples": n_samples,
        "distinct_values": n_dist,
        "studies_with_context": len(ctx_map),
        "index": os.path.abspath(args.index),
        "index_digest": file_digest(args.index),
        "embed_model": args.embed_model,
        "embed_device": dev,
        "endpoints": len(urls),
        "workers": workers,
        "merge_threshold": args.merge_threshold,
        "top_n": args.top_n,
        "use_polarity": use_pol,
        "use_picker": use_pick,
        "use_verifier": not args.no_verifier,
        "use_pubtator": not args.no_pubtator,
        "sources": dict(sources),
        "rows": n_rows,
        "platforms": n_plat,
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "host": platform.node(),
        "seconds": round(time.time() - t0, 1),
    }
    with open(os.path.join(args.out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    log("sources: " + ", ".join(f"{k}={v:,}" for k, v in sources.most_common(8)))
    log(f"total {time.time()-t0:.0f}s")
    return 0


def casefold_oov(res) -> int:
    """FINAL Phase 2 step — out-of-vocabulary surface fold.

    The cascade mints out-of-vocabulary (OOV) concepts per surface form and
    routes acronym-shaped tokens (any upper-case letter / digit) through a
    per-study short-form path that the SapBERT+LLM consolidation excludes. The
    same concept could therefore fragment across ids differing only in case,
    whitespace, or hyphenation (e.g. ``PBMC``/``pbmc``, ``wild type``/``wildtype``,
    ``Mock``/``mock``). This step folds every OOV/ART concept whose label is
    identical up to case, whitespace, hyphen, underscore, or dot onto a single
    canonical id + label (the most frequent surface form in the corpus), across
    both the top-level resolution and every per-study ``by_gse`` resolution.

    Deterministic, idempotent, and closed under the fold; touches ONLY OOV/ART
    assignments — MeSH descriptors and Cellosaurus identifiers are never changed.
    Runs in-memory on ``res`` before export, so every end-to-end pipeline run
    ships already-folded ids/labels (dictionary and per-platform CSVs).
    Returns the number of assignments re-pointed to a canonical id.
    """
    import re
    from collections import Counter, defaultdict

    def fold_key(s):
        return re.sub(r"[\s\-_\.]+", "", str(s).strip().lower())

    def is_oov(i):
        return (i or "").split(";")[0].strip().startswith(("OOV", "ART"))

    merged = 0
    for col in LABEL_COLS:
        agg = defaultdict(lambda: {"idw": Counter(), "labw": Counter()})

        def note(idv, lab, w):
            k = fold_key(lab)
            agg[k]["idw"][idv] += w
            agg[k]["labw"][lab] += w

        for raw, v in res[col].items():
            if is_oov(v.get("id")):
                note(v["id"], v.get("target") or raw, v.get("count", 1))
            for rv in (v.get("by_gse") or {}).values():
                if is_oov(rv.get("id")):
                    note(rv["id"], rv.get("target") or "", 1)
        canon = {
            k: (d["idw"].most_common(1)[0][0], d["labw"].most_common(1)[0][0])
            for k, d in agg.items()
        }
        for raw, v in res[col].items():
            if is_oov(v.get("id")):
                cid, clab = canon[fold_key(v.get("target") or raw)]
                if v["id"] != cid:
                    merged += 1
                v["id"], v["target"] = cid, clab
            for rv in (v.get("by_gse") or {}).values():
                if is_oov(rv.get("id")):
                    rv["id"], rv["target"] = canon[fold_key(rv.get("target") or "")]
    return merged


if __name__ == "__main__":
    raise SystemExit(main())
