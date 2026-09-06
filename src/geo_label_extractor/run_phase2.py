"""Phase 2 runner — dictionary normalization over a phase-1 corpus.

    python3 run_phase2.py --corpus 'p1_*.json' --out-dir . --workers 256

PIPELINE
    1. build the dictionary of distinct label values (Tissue/Condition/Treatment)
    2. resolve what is decidable without a model (junk, exact vocabulary match)
    3. resolve short forms against the study that wrote them
    4. retrieve candidates and let the model select
    5. mint a local identifier for concepts no vocabulary covers
    6. apply the finished dictionary to every sample (pure lookup, instant)
    7. export one gzipped CSV per platform

Every distinct value is decided ONCE and applied everywhere it occurs, so the
corpus is internally consistent by construction. Nothing is carried between runs;
the dictionary is written alongside the corpus as the record of what was decided.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import zlib
import json
import numpy as np
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase2_normalize import (
    LABEL_COLS,
    NS,
    Vocab,
    apply_dictionary,
    annotate_vocabulary_ids,
    canonicalize,
    build_dictionary,
    resolve_deterministic,
)
import phase2_curate
import phase2_export
import phase2_llm
import phase2_normalize

REVIEW_CONFIDENCE = float(os.environ.get("PHASE2_REVIEW_CONF", "0.60"))


def _log(msg):
    print(f"[phase2] {msg}", flush=True)


_PHASE_PREFERENCE = ("phase1b", "phase1")


def _detect_phase(paths: list[str], override: str = "") -> str:
    """Determine which phase-1 stage holds the values to normalize.

    Phase 1 may be run with or without its refinement passes, so the stage that
    carries the final extracted values differs between corpora. The choice is
    made from the corpus rather than assumed, and a stage that carries no values
    is rejected: silently normalizing an absent key would mark every sample
    Not Specified.
    """
    op = gzip.open if str(paths[0]).endswith(".gz") else open
    with op(paths[0], "rt") as fh:
        d = json.load(fh)
    rows = d["samples"] if isinstance(d, dict) and "samples" in d else d
    if not rows:
        raise SystemExit(f"corpus {paths[0]} contains no samples")

    present = {
        k
        for row in rows[:200]
        for k, v in row.items()
        if isinstance(v, dict) and set(v) & set(LABEL_COLS)
    }
    if override:
        if override not in present:
            raise SystemExit(
                f"--phase {override!r} not found in corpus; present: {sorted(present)}"
            )
        return override

    for name in _PHASE_PREFERENCE:
        if name not in present:
            continue
        populated = sum(
            1
            for row in rows[:500]
            for c in LABEL_COLS
            if str((row.get(name) or {}).get(c, "")).strip() not in ("", NS)
        )
        if populated:
            return name

    raise SystemExit(
        f"no phase-1 stage with populated labels found in {paths[0]}; "
        f"candidate keys: {sorted(present)}"
    )


_CKPT_EVERY = int(os.environ.get("PHASE2_CKPT_EVERY", "2000"))


def _assert_one_answer_per_label(res: dict, cols) -> None:
    """A phase-1 label must carry exactly one phase-2 answer.

    Two samples sharing a label must never be normalized twice, because two
    decisions on the same string can disagree and the corpus stops being
    reproducible. The dictionary is keyed by the label itself, so this holds by
    construction; it is asserted here so that any future stage which
    reintroduces a per-sample decision fails loudly instead of shipping a corpus
    whose labels silently disagree with each other.

    Short forms are the one exception, and are checked per study instead: the
    same few letters name different concepts in different experiments, so they
    are resolved from each study's own context and only then normalized through
    this one dictionary.
    """
    bad = []
    for c in cols:
        for raw, r in res.get(c, {}).items():
            t = r.get("target")
            if isinstance(t, (list, set, tuple)):
                bad.append((c, raw, t))
    if bad:
        raise SystemExit(
            "phase-2 invariant violated: %d labels carry more than one answer, "
            "e.g. %r" % (len(bad), bad[:5])
        )


def _checkpoint_path(out_dir: str) -> str:
    return os.path.join(out_dir, "phase2_decisions.ckpt.json.gz")


def _load_checkpoint(path: str, res: dict) -> tuple:
    """Apply previously written decisions. Returns (restored, short expansions).

    Short-form expansions are restored alongside the value decisions. They are
    part of the run's output, not a scratch value: without them a resumed run
    leaves every abbreviation unresolved and produces a different corpus than
    the same run completed in one go.
    """
    if not os.path.exists(path):
        return 0, {}
    try:
        with gzip.open(path, "rt") as fh:
            saved = json.load(fh)
    except (OSError, ValueError) as exc:
        _log(f"checkpoint unreadable ({exc}); starting the model passes fresh")
        return 0, {}
    n = 0
    for col, entries in saved.get("decisions", {}).items():
        for raw, rec in entries.items():
            if col in res and raw in res[col]:
                res[col][raw].update(rec)
                n += 1
    shorts = {(c, v, g): e for c, v, g, e in saved.get("shorts", [])}
    return n, shorts


_SHORTS: dict = {}


def _write_checkpoint(
    path: str,
    res: dict,
    stages: tuple = (
        "model",
        "minted",
        "reviewed",
        "reviewed-oov",
        "reviewed-none",
        "reviewed-minted",
    ),
) -> None:
    """Atomically persist every decision made by the model passes."""
    out = {}
    for col, entries in res.items():
        keep = {
            raw: {
                k: r[k]
                for k in ("target", "id", "source", "confidence", "stage")
                if k in r
            }
            for raw, r in entries.items()
            if r.get("stage") in stages
        }
        if keep:
            out[col] = keep
    tmp = path + ".tmp"
    # The caller names a directory the run may not have created yet: the
    # pipeline points normalization at <out-dir>/normalized, which exists only
    # once a shard has been written there. Losing every model decision at the
    # first checkpoint over that is a poor trade for one mkdir.
    os.makedirs(os.path.dirname(tmp) or ".", exist_ok=True)
    with gzip.open(tmp, "wt") as fh:
        json.dump(
            {
                "decisions": out,
                "shorts": [[c, v, g, e] for (c, v, g), e in sorted(_SHORTS.items())],
            },
            fh,
        )
    os.replace(tmp, path)


def _mint_by_concept(
    res: dict,
    unmatched: dict,
    retr,
    counter: dict,
    workers: int,
    threshold: float,
    n_oov: int,
) -> tuple:
    """Give every concept one identifier, however many ways it is spelled.

    Labels are decided one at a time, in an order fixed by the data: the most
    used first, ties broken by the label itself. Each is compared against the
    concepts already recorded, and the model says which of them it names, or
    that it is a new one. Deciding them in sequence is what makes the result
    transitive -- a fourth spelling joins the concept its predecessors created,
    rather than starting a rival group -- and what keeps the outcome free of any
    dependence on which labels happen to sit near each other in an array.

    Retrieval only shortlists what the model is asked about. A label with no
    near concept is new without a call, so the cost falls on the labels that
    are genuinely in question.
    """
    phase2_llm.MAX_TOKENS = int(os.environ.get("PHASE2_CONSOLIDATE_TOKENS", "2048"))
    top_n = int(os.environ.get("PHASE2_CONCEPT_CANDIDATES", "8"))
    t0 = time.time()
    n_merged = 0
    asked = 0
    n_vocab = 0
    for col in LABEL_COLS:
        vals = list(unmatched.get(col) or [])

        keep = []
        for raw in vals:
            r = res[col][raw]
            cid, mid = r.get("cell_id") or "", r.get("mesh_id") or ""
            if cid or mid:
                r.update(
                    {
                        "target": r.get("cell_name") if cid else r.get("mesh_name"),
                        "id": cid or mid,
                        "source": "cellosaurus" if cid else "mesh",
                        "stage": "exact",
                        "confidence": 1.0,
                    }
                )
                if not r.get("target"):
                    r["target"] = raw
                n_vocab += 1
            else:
                keep.append(raw)
        vals = keep
        if not vals:
            continue
        vecs = phase2_llm.embed_values(retr, vals)
        order = sorted(
            range(len(vals)),
            key=lambda i: (-(res[col][vals[i]].get("count") or 0), vals[i]),
        )
        names: list = []
        cvecs: list = []
        members: list = []
        index: dict = {}
        for pos in order:
            label = vals[pos]
            v = vecs[pos]
            pick = None
            if names:

                exact = index.get(label.strip().lower())
                if exact is not None:
                    members[exact].append(label)
                    n_merged += 1
                    continue
                sims = np.asarray(cvecs, dtype=np.float32) @ v
                near = sorted(
                    (
                        (float(sims[c]), names[c], c)
                        for c in range(len(names))
                        if float(sims[c]) >= threshold
                    ),
                    key=lambda t: (-t[0], t[1]),
                )[:top_n]
                if near:
                    asked += 1
                    shortlist = [n for _s, n, _c in near]
                    try:
                        out = phase2_llm.assign_concept(label, col, shortlist)
                    except Exception as e:
                        _log(
                            f"  concept call failed ({e.__class__.__name__}); "
                            f"{label[:40]!r} recorded as its own concept"
                        )
                        out = {"concept": None, "name": label}
                    ci = out.get("concept")
                    if isinstance(ci, int) and 0 <= ci < len(near):
                        pick = near[ci][2]
                        nm = out.get("name") or names[pick]
                        if nm != names[pick]:

                            index.pop(names[pick].strip().lower(), None)
                            names[pick] = nm
                            cvecs[pick] = phase2_llm.embed_values(retr, [nm])[0]
                            index[nm.strip().lower()] = pick
            if pick is None:
                index[label.strip().lower()] = len(names)
                names.append(label)
                cvecs.append(v)
                members.append([label])
            else:
                members[pick].append(label)
                n_merged += 1
        for ci, group in enumerate(members):
            rec = phase2_llm.mint_oov(names[ci], col, counter)
            for raw in group:
                res[col][raw].update(rec)
                res[col][raw]["stage"] = "minted"
            n_oov += 1
        _log(f"  {col}: {len(vals):,} unmatched -> {len(members):,} concepts")
    if n_vocab:
        _log(
            f"consolidation: {n_vocab :,} values kept a reference identifier "
            f"instead of being minted"
        )
    _log(
        f"consolidation: {asked :,} labels put to the model, {n_merged :,} folded "
        f"into a concept already recorded ({time.time()-t0 :.0f}s)"
    )
    return n_oov, n_merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--corpus", required=True, help="glob of phase-1 outputs, e.g. 'p1_*.json'"
    )
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--vocab", default="/dev/shm/mesh2026/vocab.sqlite")
    ap.add_argument("--index", default="/dev/shm/mesh2026/vocab_index.npz")
    ap.add_argument("--cellosaurus", default="cellosaurus.sqlite")
    ap.add_argument("--workers", type=int, default=256)
    ap.add_argument("--topk", type=int, default=8)
    ap.add_argument(
        "--merge-threshold",
        type=float,
        default=0.90,
        help="similarity floor for shortlisting the concepts a "
        "label is compared against; the model makes the "
        "decision, this only sets what it is asked about. "
        "Measured on this corpus against labels differing "
        "only in capitalisation and spacing: recall of those "
        "rises to 99.7% by 0.90 and does not improve above it, "
        "while a lower floor buys nothing and costs calls",
    )
    ap.add_argument(
        "--phase",
        default="",
        help="phase-1 stage to normalize; detected from the " "corpus when omitted",
    )
    ap.add_argument(
        "--escalate",
        action="store_true",
        help="re-decide unresolved values with a larger reasoning "
        "budget; off by default, the prompt carries the rules "
        "the second pass used to supply",
    )
    ap.add_argument(
        "--no-t4", action="store_true", help="skip the optional review pass"
    )
    ap.add_argument(
        "--only-col",
        default="",
        help="resolve a single label column; the columns are "
        "independent, so they may be run as separate jobs and "
        "their dictionaries merged before applying",
    )
    ap.add_argument(
        "--shard",
        default="",
        help="resolve only part of the dictionary, as i/N. Values "
        "are assigned to shards by a stable hash of the value, "
        "so shards are disjoint and their dictionaries merge "
        "without overlap",
    )
    ap.add_argument(
        "--no-export",
        action="store_true",
        help="stop after applying labels; skip the per-platform " "table export",
    )
    ap.add_argument(
        "--export-dir",
        default="",
        help="where the per-platform tables are written " "(default: <out-dir>/final)",
    )
    ap.add_argument(
        "--dict-only",
        action="store_true",
        help="resolve the dictionary and stop, do not apply",
    )
    ap.add_argument(
        "--no-curate",
        action="store_true",
        help="skip the curation review of finished assignments",
    )
    ap.add_argument(
        "--curate-only",
        action="store_true",
        help="review an existing dictionary without re-normalizing",
    )
    args = ap.parse_args()

    global LABEL_COLS
    if args.only_col:
        want = tuple(x.strip() for x in args.only_col.split(",") if x.strip())
        bad = [x for x in want if x not in LABEL_COLS]
        if bad:
            raise SystemExit(f"--only-col {bad} not in {LABEL_COLS}")
        LABEL_COLS = want
        phase2_normalize.LABEL_COLS = want
        _log(f"restricted to columns: {want}")

    paths = sorted(
        p
        for p in glob.glob(args.corpus)
        if not re.search(r"\.(partial|tmp|ckpt)\b", os.path.basename(p))
    )
    if not paths:
        _log(f"no corpus files matched {args.corpus!r}")
        return 2
    _log(f"corpus: {len(paths)} files")
    phase = _detect_phase(paths, args.phase)
    _log(f"source stage: {phase}")

    t0 = time.time()
    dictionary = build_dictionary(paths, phase=phase)
    if args.shard:
        si, sn = (int(x) for x in args.shard.split("/"))
        keep = lambda k: zlib.crc32(k.encode()) % sn == si
        for c in LABEL_COLS:
            dictionary["freq"][c] = {
                k: v for k, v in dictionary["freq"][c].items() if keep(k)
            }
            dictionary.setdefault("shorts", {})[c] = {
                k: v
                for k, v in dictionary.get("shorts", {}).get(c, {}).items()
                if keep(k)
            }
        _log(
            f"shard {si}/{sn}: "
            + ", ".join(f"{c}={len(dictionary['freq'][c]):,}" for c in LABEL_COLS)
        )

    n_dist = sum(len(dictionary["freq"][c]) for c in LABEL_COLS)
    n_inst = sum(sum(dictionary["freq"][c].values()) for c in LABEL_COLS)
    _log(
        f"dictionary: {n_dist :,} distinct values over {n_inst :,} label-instances "
        f"from {dictionary['n_samples']:,} samples ({time.time()-t0 :.0f}s)"
    )

    t0 = time.time()
    vocab = Vocab.load(args.vocab, args.cellosaurus)
    _log(
        f"vocabulary: {len(vocab.mesh):,} mesh keys, {len(vocab.cells):,} cell-line keys"
    )
    det = resolve_deterministic(dictionary, vocab)
    res = det["resolutions"]
    pending = [
        (c, raw)
        for c in LABEL_COLS
        for raw, r in res[c].items()
        if r["stage"] == "pending"
    ]
    _log(
        f"deterministic done in {time.time()-t0 :.0f}s — {len(pending):,} values "
        f"pending for the model"
    )
    ckpt_path = _checkpoint_path(args.out_dir)
    restored, restored_shorts = _load_checkpoint(ckpt_path, res)
    if restored_shorts:
        _SHORTS.update(restored_shorts)
        _log(f"resumed {len(restored_shorts):,} short-form expansions")
    if restored:
        pending = [
            (c, raw)
            for c in LABEL_COLS
            for raw, r in res[c].items()
            if r["stage"] == "pending"
        ]
        _log(
            f"resumed {restored :,} decisions from checkpoint; "
            f"{len(pending):,} values still pending"
        )

    if args.curate_only:
        prior = os.path.join(args.out_dir, "phase2_dictionary.json.gz")
        if not os.path.exists(prior):
            raise SystemExit("--curate-only needs %s" % prior)
        with gzip.open(prior, "rt") as fh:
            saved = json.load(fh)["resolutions"]
        restored = 0
        for col in LABEL_COLS:
            for raw, rec in saved.get(col, {}).items():
                res.setdefault(col, {}).setdefault(raw, {}).update(rec)
                restored += 1
        _log("curate-only: loaded %d prior assignments" % restored)
        pending = []

    needs_model = (not args.curate_only) and (
        bool(pending)
        or any(
            r.get("stage") in ("model", "pending-short") and r.get("target") is None
            for c in LABEL_COLS
            for r in res[c].values()
        )
    )
    shorts_map: dict = dict(_SHORTS)
    if needs_model:
        phase2_llm.MAX_TOKENS = int(os.environ.get("PHASE2_SHORT_TOKENS", "2048"))
        sh = dictionary.get("shorts", {})
        short_items = [
            (col, raw, gse, slot.get("text", ""))
            for col in LABEL_COLS
            for raw, r in res[col].items()
            if r.get("stage") == "pending-short"
            for gse, slot in sorted(sh.get(col, {}).get(raw, {}).items())
            if (col, raw, gse) not in shorts_map
        ]
        if short_items:
            t0 = time.time()
            slock = threading.Lock()
            sstats = Counter()
            sdone = [0]

            def expand(item):
                col, raw, gse, ctx = item
                try:
                    out = phase2_llm.expand_short(raw, col, ctx)
                except Exception as e:
                    out = {
                        "expansion": None,
                        "source": "error:%s" % e.__class__.__name__,
                    }
                with slock:
                    if out.get("expansion"):
                        shorts_map[(col, raw, gse)] = out["expansion"]
                        _SHORTS[(col, raw, gse)] = out["expansion"]
                    sstats[out.get("source", "?")] += 1
                    sdone[0] += 1
                    if sdone[0] % 1000 == 0:
                        _log(f"  shorts {sdone[0]:,}/{len(short_items):,}")

            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                list(ex.map(expand, short_items))
            _log(
                f"shorts: {len(short_items):,} (value,study) pairs -> "
                f"{len(shorts_map):,} resolved in {time.time()-t0 :.0f}s "
                f"{dict(sstats)}"
            )
            _write_checkpoint(ckpt_path, res)

        for (col, raw, gse), exp in sorted(shorts_map.items()):
            if col not in res:
                continue
            if exp not in res[col]:
                res[col][exp] = {
                    "stage": "pending",
                    "canon": canonicalize(exp),
                    "target": None,
                    "id": "",
                    "source": "",
                    "count": 0,
                }
                pending.append((col, exp))
            res[col][exp]["count"] += (
                sh.get(col, {}).get(raw, {}).get(gse, {}).get("count", 0)
            )

        returned = 0
        for col in LABEL_COLS:
            for raw, r in res[col].items():
                if r.get("stage") == "pending-short":
                    r["stage"] = "pending"
                    pending.append((col, raw))
                    returned += 1
        if returned:
            _log(f"unexpanded short forms returned to selection: {returned :,}")

        retr = phase2_llm.Retriever(args.index)
        _log(f"retrieval index: {len(retr.forms):,} vectors")

        cand_map: dict = {}
        for col in LABEL_COLS:
            todo = sorted(
                {raw for c, raw in pending if c == col}
                | {
                    raw
                    for raw, r in res[col].items()
                    if r.get("stage") in ("model",) and r.get("target") is None
                }
            )
            if not todo:
                continue
            t0 = time.time()
            queries = [res[col][raw]["canon"] for raw in todo]
            for chunk_start in range(0, len(todo), 4096):
                chunk = todo[chunk_start : chunk_start + 4096]
                qs = queries[chunk_start : chunk_start + 4096]
                for raw, cands in zip(chunk, retr.search(qs, col, k=args.topk)):
                    cand_map[(col, raw)] = cands
            _log(
                f"  retrieved candidates for {len(todo):,} {col} values "
                f"({time.time()-t0 :.0f}s)"
            )

        phase2_llm.THINK = True
        phase2_llm.MAX_TOKENS = int(os.environ.get("PHASE2_MAX_TOKENS", "4096"))
        stats = Counter()
        lock = threading.Lock()
        done = [0]
        t0 = time.time()

        def pick(item):
            col, raw = item
            cands = cand_map.get((col, raw), [])
            try:
                out = phase2_llm.normalize_one(raw, col, cands)
            except Exception as e:
                out = {
                    "target": None,
                    "id": "",
                    "source": f"error:{e.__class__.__name__}",
                    "confidence": 0.0,
                }
            with lock:
                res[col][raw].update(out)
                res[col][raw]["stage"] = "model"
                stats[out["source"]] += 1
                done[0] += 1
                if done[0] % _CKPT_EVERY == 0:
                    _write_checkpoint(ckpt_path, res)
                if done[0] % 5000 == 0:
                    r = done[0] / max(time.time() - t0, 1e-6)
                    _log(f"  selected {done[0]:,}/{len(pending):,} ({r :.0f}/s)")

        if pending:
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                list(ex.map(pick, pending))
        _write_checkpoint(ckpt_path, res)
        _log(f"selection done in {time.time()-t0 :.0f}s — {dict(stats)}")

        if args.escalate and not args.no_t4:
            hard = [
                (c, raw)
                for c in LABEL_COLS
                for raw, r in res[c].items()
                if r.get("stage") in ("model",)
                and (
                    r.get("target") is None
                    or float(r.get("confidence") or 0) < REVIEW_CONFIDENCE
                )
            ]
            _log(
                f"review: {len(hard):,} values re-decided with reasoning "
                f"({100 * len(hard)/max(len(pending),1):.1f}%)"
            )
            if hard:
                phase2_llm.THINK = True
                phase2_llm.MAX_TOKENS = int(os.environ.get("PHASE2_T4_TOKENS", "6144"))
                phase2_llm.THINK_BUDGET = int(
                    os.environ.get("PHASE2_T4_THINK_BUDGET", "1024")
                )
                done[0] = 0
                t0 = time.time()
                stats4 = Counter()

                def rethink(item):
                    col, raw = item
                    cands = cand_map.get((col, raw), [])
                    try:
                        out = phase2_llm.normalize_one(raw, col, cands)
                    except Exception as e:
                        out = {
                            "target": None,
                            "id": "",
                            "source": f"error:{e.__class__.__name__}",
                            "confidence": 0.0,
                        }
                    with lock:
                        if (
                            out.get("target") is not None
                            or res[col][raw].get("target") is None
                        ):
                            res[col][raw].update(out)
                            res[col][raw]["stage"] = "model"
                        stats4[out["source"]] += 1
                        done[0] += 1
                        if done[0] % _CKPT_EVERY == 0:
                            _write_checkpoint(ckpt_path, res)
                        if done[0] % 1000 == 0:
                            r = done[0] / max(time.time() - t0, 1e-6)
                            _log(
                                f"  reviewed {done[0]:,}/{len(hard):,} ({r :.1f}/s)"
                            )

                with ThreadPoolExecutor(max_workers=args.workers) as ex:
                    list(ex.map(rethink, hard))
                _write_checkpoint(ckpt_path, res)
                _log(f"review done in {time.time()-t0 :.0f}s — {dict(stats4)}")

        vocab_hits = annotate_vocabulary_ids(res, vocab)
        _log(f"vocabulary coverage: {vocab_hits}")

        counter: dict = {}
        n_oov = 0
        n_recovered = 0
        n_failed = 0
        failed_examples = []

        recover = []
        for col in LABEL_COLS:
            for raw, r in res[col].items():
                if r.get("target") is not None:
                    continue
                if r.get("cell_id") or r.get("mesh_id"):
                    recover.append((col, raw))

        if recover:
            t0 = time.time()
            rlock = threading.Lock()
            rdone = [0]

            def confirm(item):
                col, raw = item
                r = res[col][raw]
                cands = []
                if r.get("cell_id"):
                    cands.append(
                        phase2_llm.Candidate(
                            raw,
                            r["cell_id"],
                            r.get("cell_name") or raw,
                            "",
                            "cellosaurus",
                            1.0,
                        )
                    )
                if r.get("mesh_id"):
                    cands.append(
                        phase2_llm.Candidate(
                            raw,
                            r["mesh_id"],
                            r.get("mesh_name") or raw,
                            "",
                            "mesh",
                            1.0,
                        )
                    )
                try:
                    out = phase2_llm.normalize_one(raw, col, cands)
                except Exception:
                    out = {"target": None}
                with rlock:
                    if out.get("target"):
                        r.update(out)
                        r["stage"] = "exact"
                    rdone[0] += 1
                    if rdone[0] % 2000 == 0:
                        _log(f"  vocabulary recovery {rdone[0]:,}/{len(recover):,}")

            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                list(ex.map(confirm, recover))
            n_recovered = sum(
                1 for col, raw in recover if res[col][raw].get("stage") == "exact"
            )
            _log(
                f"vocabulary recovery: {len(recover):,} asked, {n_recovered :,} "
                f"confirmed in {time.time()-t0 :.0f}s"
            )

        unmatched: dict = {c: [] for c in LABEL_COLS}
        for col in LABEL_COLS:
            for raw, r in res[col].items():
                if r.get("target") is not None:
                    continue
                if r.get("source") == "llm-oov":
                    unmatched[col].append(raw)
                else:
                    r["stage"] = "FAILED"
                    n_failed += 1
                    if len(failed_examples) < 10:
                        failed_examples.append(
                            f"{col}:{raw[:40]}={r.get('source')}"
                        )

        n_oov, n_merged = _mint_by_concept(
            res, unmatched, retr, counter, args.workers, args.merge_threshold, n_oov
        )
        _log(
            f"minted {n_oov :,} identifiers for genuinely uncovered concepts; "
            f"{n_recovered :,} recovered from a vocabulary the selection missed"
        )

        _write_checkpoint(ckpt_path, res)
        if n_failed:
            inst = sum(
                r["count"]
                for col in LABEL_COLS
                for r in res[col].values()
                if r["stage"] == "FAILED"
            )
            _log(
                f"!! {n_failed :,} values UNRESOLVED BY FAILURE "
                f"({inst :,} sample-label instances). These keep their phase-1 "
                f"value verbatim and are NOT normalized."
            )
            _log(f"!! examples: {failed_examples}")

    if not args.no_curate:
        curate_retr = retr if "retr" in dir() else phase2_llm.Retriever(args.index)
        ckpt_path = (
            ckpt_path if "ckpt_path" in dir() else _checkpoint_path(args.out_dir)
        )
        phase2_llm.THINK = True
        phase2_llm.MAX_TOKENS = int(os.environ.get("CURATE_MAX_TOKENS", "4096"))
        phase2_llm.THINK_BUDGET = int(os.environ.get("CURATE_THINK_BUDGET", "256"))
        phase2_curate.review_values(
            res,
            curate_retr,
            LABEL_COLS,
            workers=args.workers,
            topk=args.topk,
            log=_log,
            checkpoint=lambda r: _write_checkpoint(ckpt_path, r),
            cache_dir=args.out_dir,
            index_path=args.index,
        )
        phase2_curate.review_clusters(
            res,
            LABEL_COLS,
            workers=args.workers,
            log=_log,
            checkpoint=lambda r: _write_checkpoint(ckpt_path, r),
        )
        counter2: dict = {}
        leftover = {
            c: [
                raw
                for raw, r in res[c].items()
                if r.get("target") is None and r.get("curated")
            ]
            for c in LABEL_COLS
        }
        minted, _folded = _mint_by_concept(
            res, leftover, curate_retr, counter2, args.workers, args.merge_threshold, 0
        )
        for col in LABEL_COLS:
            for raw in leftover[col]:
                res[col][raw]["curated"] = True
        _log(f"curation: minted {minted :,} identifiers for reviewed-out values")
        reasons, unreviewed = phase2_curate.failure_report()
        if reasons:
            _log(f"curation failures by reason: {reasons}")
            _log(
                f"values left unreviewed: {len(unreviewed):,} "
                f"({100 * len(unreviewed)/max(sum(len(res[c]) for c in LABEL_COLS),1):.1f}%)"
            )
            with open(os.path.join(args.out_dir, "curation_failures.json"), "w") as fh:
                json.dump(
                    {"reasons": reasons, "unreviewed": [list(x) for x in unreviewed]},
                    fh,
                )
        phase2_curate.report(res, LABEL_COLS, log=_log)
        _write_checkpoint(ckpt_path, res)

    os.makedirs(args.out_dir, exist_ok=True)
    dict_path = os.path.join(args.out_dir, "phase2_dictionary.json.gz")
    with gzip.open(dict_path, "wt") as fh:
        json.dump(
            {
                "n_samples": dictionary["n_samples"],
                "resolutions": res,
                "shorts": [[c, v, g, e] for (c, v, g), e in sorted(shorts_map.items())],
            },
            fh,
        )
    _log(f"wrote {dict_path}")

    for col in LABEL_COLS:
        stages = Counter(r["stage"] for r in res[col].values())
        inst = Counter()
        for r in res[col].values():
            inst[r["stage"]] += r["count"]
        _log(
            f"  {col :<10} "
            + "  ".join(
                f"{t}={stages[t]:,}({100 * inst[t]/max(sum(inst.values()),1):.0f}% inst)"
                for t in sorted(stages)
            )
        )

    if args.dict_only:
        return 0

    _log(f"vocabulary coverage (final): {annotate_vocabulary_ids(res ,vocab)}")
    _assert_one_answer_per_label(res, LABEL_COLS)
    applied = []
    seen_targets: dict = {}
    used_names: set = set()
    for p in paths:
        t0 = time.time()
        out_p = os.path.join(args.out_dir, os.path.basename(p).replace("p1_", "p2_"))
        # The p1_*.json convention gives every input a distinct name, but a
        # corpus assembled another way need not: shards written as
        # <shard>/extracted.json all reduce to the same output name, and each
        # silently overwrites the last. One run lost 360,000 of 602,900 samples
        # that way -- the work was done, the file was not kept. Disambiguate
        # with the input's own directory, which is what made the paths unique.
        if out_p in used_names:
            parent = os.path.basename(os.path.dirname(os.path.abspath(p))) or "shard"
            stem, ext = os.path.splitext(os.path.basename(out_p))
            out_p = os.path.join(args.out_dir, f"{stem}__{parent}{ext}")
        used_names.add(out_p)
        counts = apply_dictionary(
            p, res, out_p, phase_in=phase, shorts=shorts_map, seen=seen_targets
        )
        applied.append(out_p)
        _log(
            f"applied {os.path.basename(p)} -> {os.path.basename(out_p)} "
            f"({time.time()-t0 :.0f}s) {dict(counts)}"
        )

    _log(
        f"reproducibility: {len(seen_targets):,} distinct (field, label) pairs, "
        f"each carrying exactly one answer across all {len(paths)} shards"
    )

    if args.no_export:
        _log("export skipped (--no-export)")
    else:
        t0 = time.time()
        export_dir = args.export_dir or os.path.join(args.out_dir, "final")
        os.makedirs(export_dir, exist_ok=True)
        per_platform = phase2_export.export(applied, dict_path, export_dir, log=_log)
        _log(
            f"exported {len(per_platform):,} platforms, "
            f"{sum(per_platform.values()):,} samples -> {export_dir} "
            f"({time.time()-t0 :.0f}s)"
        )

    _log("PHASE2 COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
