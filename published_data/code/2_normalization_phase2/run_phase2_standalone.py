"""Standalone Phase 2 pass over an existing pipeline_out.json.

Why a separate driver: run_cli.py's Phase 2 path uses the agent fleet
(Coordinator + collapsers + verifier) which deadlocks under high
parallelism. Phase2Mesh.collapse_record bypasses the agent fleet and
calls MeshDB / the LLM picker / verifier directly.

Input: a pipeline_out.json from a Phase 1+1b+1c run (--no-p2). Each row
must carry phase1b with {"Tissue","Condition","Treatment"}. Phase2Mesh
is fed the phase1b labels; resolved canonical names go back into
``phase2`` (overwriting the no-op copy-of-phase1b written by --no-p2).

Output: a new JSON next to the input with canonical phase2 + audit IDs.
"""

from __future__ import annotations

import argparse
import json
import os
import resource as _resource
import sys
import threading
import time

try:
    _fd_soft, _fd_hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
    _resource.setrlimit(_resource.RLIMIT_NOFILE, (_fd_hard, _fd_hard))
except Exception:
    pass
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from phase2_mesh import LABEL_COLS, NS, Phase2Mesh, _extract_dose
from mesh_lookup import MeshDB

try:
    from gse_context_cache import GSEContextCache
except Exception:
    GSEContextCache = None


def _load_gse_context_map(*paths: str) -> dict[str, str]:
    """Return {gse_id: 'title\\nsummary\\noverall_design'} from the first
    JSON file that exists. Empty dict if none."""
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        with open(p) as fh:
            meta = json.load(fh)
        out: dict[str, str] = {}
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
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--input", required=True, help="pipeline_out.json from Phase 1+1b+1c (--no-p2)"
    )
    ap.add_argument(
        "--output", required=True, help="where to write Phase-2-augmented JSON"
    )
    ap.add_argument(
        "--gse-meta",
        default="",
        help="gse_meta_scraped.json (or gse_meta.json) for context",
    )
    ap.add_argument(
        "--cache-db",
        default="gse_context_cache.sqlite",
        help="GSEContextCache for sibling consistency",
    )
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--no-pubtator", action="store_true", help="disable PubTator3 (offline mode)"
    )
    ap.add_argument(
        "--no-verifier",
        action="store_true",
        help="disable LLM verifier (faster, less safe)",
    )
    args = ap.parse_args()

    raw = json.loads(Path(args.input).read_text())

    if isinstance(raw, dict) and "samples" in raw:
        rows = raw["samples"]
        wrap = {k: v for k, v in raw.items() if k != "samples"}
    elif isinstance(raw, list):
        rows = raw
        wrap = None
    else:
        print("unrecognized input shape", file=sys.stderr)
        return 2
    print(f"[p2-standalone] {len(rows)} rows", flush=True)

    ctx_map = _load_gse_context_map(args.gse_meta) if args.gse_meta else {}
    print(f"[p2-standalone] {len(ctx_map)} GSEs with scraped context", flush=True)

    cache = None
    if GSEContextCache is not None and os.path.exists(args.cache_db):
        try:
            cache = GSEContextCache(args.cache_db)
        except Exception as e:
            print(f"[p2-standalone] cache disabled: {e!r}", flush=True)

    try:
        MeshDB.verify_pipeline_health(
            ollama_url=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"),
            model_name=os.environ.get("PHASE2_MODEL", "gemma4-e2b-text:latest"),
            gse_cache_db=args.cache_db,
            require_biolord=True,
            strict=True,
        )
        print("[p2-standalone] preflight OK", flush=True)
    except RuntimeError as _e:
        print(f"[p2-standalone] PREFLIGHT FAILED: {_e}", flush=True)
        raise

    p2 = Phase2Mesh(
        db=MeshDB(),
        use_pubtator=not args.no_pubtator,
        use_verifier=not args.no_verifier,
        cache=cache,
        use_polarity=(os.environ.get("PHASE2_USE_POLARITY", "0") != "0"),
        use_picker=(os.environ.get("PHASE2_USE_PICKER", "0") != "0"),
    )

    if cache is not None:
        from collections import defaultdict as _dd

        _unique: dict = _dd(set)
        for r in rows:
            gid = r.get("gse")
            if not gid:
                continue
            _p1b = r.get("phase1b") or {}
            for _c in LABEL_COLS:
                _raw = (_p1b.get(_c) or "").strip()
                if _raw and _raw.lower() != NS.lower():
                    _unique[gid].add((_raw, _c))
        _n_uniq = sum(len(s) for s in _unique.values())
        print(
            f"[p2-standalone] warm-up: {_n_uniq} unique (raw,col) "
            f"across {len(_unique)} GSEs — serial pass to seed "
            f"per-GSE sibling cache",
            flush=True,
        )
        _t_warm = time.time()
        _w_done = [0]
        _w_lock = threading.Lock()

        def _warm_gse(_gid, _raws):
            _ctx = ctx_map.get(_gid, "")
            for _raw, _col in sorted(_raws):
                try:
                    p2.collapse(_raw, _col, context=_ctx, gse_id=_gid)
                except Exception as e:
                    print(
                        f"[p2-standalone] warm-up err {_gid}/{_raw!r}/{_col}: "
                        f"{e!r}",
                        flush=True,
                    )
                with _w_lock:
                    _w_done[0] += 1
                    _d = _w_done[0]
                if _d % 100 == 0:
                    _rate = _d / max(time.time() - _t_warm, 1e-6)
                    print(
                        f"[p2-standalone] warm-up {_d}/{_n_uniq} "
                        f"({_rate :.2f}/s)",
                        flush=True,
                    )

        with ThreadPoolExecutor(max_workers=args.workers) as _wex:
            _wfuts = [
                _wex.submit(_warm_gse, _gid, _raws)
                for _gid, _raws in sorted(_unique.items())
            ]
            for _f in as_completed(_wfuts):
                _f.result()
        print(
            f"[p2-standalone] warm-up done in {time.time()-_t_warm :.1f}s", flush=True
        )

    _err_n = [0]
    _ERR_FRAC = 0.25

    def _do_one(idx: int, r: dict) -> tuple[int, dict]:
        gse_id = r.get("gse")
        p1b = r.get("phase1b") or {}

        rec = {
            "gse": gse_id,
            "gse_context": "",
            **{
                k: r.get(k, "")
                for k in (
                    "characteristics",
                    "source",
                    "title",
                    "treatment_protocol",
                    "description",
                    "characteristics_ch1",
                    "source_name_ch1",
                    "treatment_protocol_ch1",
                )
            },
            **{c: (p1b.get(c) or NS) for c in LABEL_COLS},
        }
        try:
            res = p2.collapse_record(rec)
        except Exception as e:

            print(
                f"[p2-standalone] {gse_id}/{r.get('gsm')} err: {e!r}", flush=True
            )
            new_row = dict(r)
            new_row["phase2"] = {c: NS for c in LABEL_COLS}

            for c in ("Sex", "Age"):
                new_row["phase2"][c] = p1b.get(c, NS)
            new_row["phase2"]["Treatment_dose"] = _extract_dose(
                p1b.get("Treatment", "") or ""
            )
            new_row["phase2_id"] = {c: "" for c in LABEL_COLS}
            new_row["phase2_error"] = repr(e)[:200]
            return idx, new_row
        new_row = dict(r)
        new_row["phase2"] = {c: res.get(c, p1b.get(c, NS)) for c in LABEL_COLS}

        for c in ("Sex", "Age"):
            new_row["phase2"][c] = p1b.get(c, NS)

        new_row["phase2"]["Treatment_dose"] = res.get("Treatment_dose", "")
        new_row["phase2_id"] = {c: res.get(f"{c}_id", "") for c in LABEL_COLS}
        return idx, new_row

    out = list(rows)
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_do_one, i, r) for i, r in enumerate(rows)]
        for f in as_completed(futs):
            idx, new_row = f.result()
            out[idx] = new_row
            done += 1

            if "phase2_error" in new_row:
                _err_n[0] += 1

            if done % 25 == 0 or done == len(rows):
                if _err_n[0] / max(done, 1) > _ERR_FRAC:
                    raise RuntimeError(
                        f"[p2-standalone] error rate {_err_n[0]}/{done} "
                        f"exceeds {_ERR_FRAC :.0%}. Aborting before "
                        f"more samples get tagged with stale phase2 NS. "
                        f"Likely GPU OOM (Ollama hogging VRAM) or DB "
                        f"corruption. Run: pkill -f 'ollama runner' "
                        f"and re-run preflight."
                    )
                rate = done / max(time.time() - t0, 1e-6)
                eta = (len(rows) - done) / max(rate, 1e-6)
                print(
                    f"[p2-standalone] {done}/{len(rows)} "
                    f"({rate :.2f}/s, eta {eta / 60 :.1f} min)",
                    flush=True,
                )

    if wrap is not None:
        payload = {**wrap, "samples": out, "n_samples": len(out)}
        Path(args.output).write_text(json.dumps(payload, indent=2, default=str))
    else:
        Path(args.output).write_text(json.dumps(out, indent=2, default=str))
    if cache is not None:
        try:
            n = p2.promote_global_canons(min_gses=3)
            print(f"[p2-standalone] promoted {n} cross-GSE canons", flush=True)
        except Exception as e:
            print(f"[p2-standalone] promote skipped: {e!r}", flush=True)
    print(f"[p2-standalone] wrote {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
