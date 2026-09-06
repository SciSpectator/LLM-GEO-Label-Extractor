"""LLM-GEO-Label-Extractor — headless CLI runner (no GUI, cluster-friendly).

Same pipeline as ``llm_label_extractor.py`` but driven entirely by command-line
arguments and stdout / stderr logging. No tkinter import, no display required.
Designed for SLURM / PBS submission via ``submit.sh``.

Phases run for each GSE in turn:

    Phase 1   verbatim per-label LLM extraction
    Phase 1b  GSE-context inference for "Not Specified" fields
    Phase 2   multi-agent MeSH cascade canonicalisation

Inputs
------
  --samples samples.json   list of {gsm, title, source_name_ch1,
                           characteristics_ch1, treatment_protocol_ch1,
                           description, gse} dicts
  --gpl GPLxxxx            (optional) dump every GSM on this platform from
                           GEOmetadb.sqlite into ``samples.json`` first
  --output out.json        final merged JSON written at the end
  --checkpoint ckpt.jsonl  per-GSE JSONL append; rerun resumes from here

Run options
-----------
  --workers N              N collapsers + 1 verifier (1..3, default 1)
  --backend ollama|vllm    LLM backend (default ollama)
  --model NAME             model id (default gemma4-e2b-text:latest)
  --no-scrape              skip NCBI GSE-meta scrape (use existing sidecar)
  --no-p1 / --no-p1b /     turn off any phase
  --no-p2

Example
-------
  python run_cli.py --samples samples.json \\
                    --output  out.json    \\
                    --checkpoint ckpt.jsonl \\
                    --workers 1
"""

from __future__ import annotations

import argparse
import json
import os
import resource as _resource
import signal
import sqlite3
import sys
import time

try:
    _fd_soft, _fd_hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
    _resource.setrlimit(_resource.RLIMIT_NOFILE, (_fd_hard, _fd_hard))
    print(f"[run] RLIMIT_NOFILE raised {_fd_soft} -> {_fd_hard}", flush=True)
except Exception as _e:
    print(f"[run] could not raise RLIMIT_NOFILE: {_e!r}", flush=True)
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _pmap(fn, items, workers):
    """Parallel map preserving input order. Falls back to serial when workers<=1."""
    items = list(items)
    if workers <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


MAX_WORKERS = 32
DEFAULT_WORKERS = 1
DEFAULT_MODEL = "gemma4-e2b-text:latest"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_VLLM_URL = "http://localhost:8000/v1"
DEFAULT_LLM_BACKEND = "ollama"
LABEL_COLS = ("Tissue", "Condition", "Treatment")
EXTRA_LABEL_COLS = ("Sex", "Age")
ALL_LABEL_COLS = LABEL_COLS + EXTRA_LABEL_COLS
NS = "Not Specified"


_P1_MAX_ATTEMPTS = int(os.environ.get("P1_MAX_ATTEMPTS", "12"))
_P1_BACKOFF_START = float(os.environ.get("P1_BACKOFF_START", "2"))
_P1_MAX_BACKOFF = float(os.environ.get("P1_MAX_BACKOFF", "60"))


class _P1Failed:
    """Sentinel: this sample could not be extracted for infrastructure reasons.

    It is dropped from the batch rather than written with any value. The resume
    unit is the sample (see ``_read_checkpoint``), so the next pass picks it up.
    """


_P1_FAILED = _P1Failed()


def _active_labels() -> tuple:
    """Return the user-selected subset of ALL_LABEL_COLS, or all if unset.

    Driven by the ``ACTIVE_LABELS`` env var (CSV, e.g. 'Sex,Age').
    Case-sensitive — names must match ALL_LABEL_COLS exactly.
    """
    sel = os.environ.get("ACTIVE_LABELS", "").strip()
    if not sel:
        return ALL_LABEL_COLS
    asked = {s.strip() for s in sel.split(",") if s.strip()}
    return tuple(c for c in ALL_LABEL_COLS if c in asked)


def _active_label_cols() -> tuple:
    """Active labels ∩ LABEL_COLS — what Phase 1b/1c-consensus/2 act on
    (Phase 1b/2 do not handle Sex/Age; Phase 1b handles them via its
    own validator branch but the consensus rules run on LABEL_COLS).
    """
    return tuple(c for c in LABEL_COLS if c in _active_labels())


SNAPSHOT_EVERY_N = 100

HERE = Path(__file__).resolve().parent
PARENT = HERE.parent
sys.path.insert(0, str(HERE))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_cli.py", description="LLM-GEO-Label-Extractor headless CLI runner."
    )
    p.add_argument(
        "--samples",
        required=False,
        type=Path,
        help="input samples.json (skip if --gpl is set with --dump-only)",
    )
    p.add_argument(
        "--output",
        required=False,
        type=Path,
        help="final merged output JSON (omit with --search-gpl)",
    )
    p.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="per-GSE JSONL append for crash-resume",
    )
    p.add_argument(
        "--gpl",
        default=None,
        help="optional GPL id; dumps all GSMs into --samples first",
    )
    p.add_argument(
        "--search-gpl",
        default=None,
        metavar="QUERY",
        help="search GEOmetadb for matching GPLs and print them. "
        "QUERY can be a GPL id (e.g. GPL570), an organism "
        "(e.g. 'homo sapiens'), or a fragment of the platform "
        "title. Combine with --tech to filter by technology. "
        "Standalone mode — exits after printing.",
    )
    p.add_argument(
        "--tech",
        default=None,
        help="technology filter for --search-gpl (e.g. "
        "'in situ oligonucleotide', 'high-throughput sequencing')",
    )
    p.add_argument(
        "--organism",
        default=None,
        help="organism / species filter for --search-gpl "
        "(e.g. 'Homo sapiens', 'Mus musculus')",
    )
    p.add_argument(
        "--list-gpl-limit",
        type=int,
        default=50,
        help="max rows for --search-gpl (default 50)",
    )
    p.add_argument(
        "--geometadb",
        type=Path,
        default=None,
        help="path to GEOmetadb.sqlite (for --gpl or --search-gpl)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"1..{MAX_WORKERS} (default {DEFAULT_WORKERS})",
    )
    p.add_argument(
        "--gse-workers",
        type=int,
        default=1,
        help="how many GSEs to process concurrently (default 1; "
        "raise ONLY if Phase 2 is disabled or the agent fleet "
        "is verified deadlock-free on your hardware)",
    )
    p.add_argument(
        "--backend",
        default=DEFAULT_LLM_BACKEND,
        choices=["ollama", "vllm", "sglang", "openai"],
    )
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--vllm-url", default=DEFAULT_VLLM_URL)
    p.add_argument(
        "--limit", type=int, default=0, help="cap input to first N samples (0 = all)"
    )
    p.add_argument(
        "--no-resume", action="store_true", help="ignore existing checkpoint"
    )
    p.add_argument("--no-scrape", action="store_true")
    p.add_argument("--no-p1", action="store_true")
    p.add_argument("--no-p1b", action="store_true")
    p.add_argument(
        "--no-p1c",
        action="store_true",
        help="accepted for compatibility; phase 1c no longer exists",
    )
    p.add_argument("--no-p2", action="store_true")

    p.add_argument(
        "--no-think",
        action="store_true",
        help="also disable gemma4 reasoning for phase 2 (think:false "
        "everywhere). Default: reasoning OFF for phase 1/1b/1c, "
        "ON for phase 2 only.",
    )
    p.add_argument(
        "--labels",
        default="",
        help="CSV subset of labels to extract — choose from "
        f"{','.join(ALL_LABEL_COLS)} "
        "(default: all five). Sex/Age are demographics-only "
        "(Phase 1 + Phase 1b validator); Phase 1b/2 only "
        "run on Tissue/Condition/Treatment. "
        "Example: --labels Sex,Age runs ONLY demographics; "
        "--labels Tissue,Condition runs only those two "
        "through every phase. Names are case-sensitive.",
    )
    args = p.parse_args()

    _p2_think = "false" if args.no_think else "true"
    os.environ.setdefault("PHASE1_THINK", "false")
    os.environ.setdefault("PHASE1B_THINK", "false")
    os.environ.setdefault("PHASE1C_THINK", "false")
    os.environ["PHASE2_THINK"] = _p2_think
    os.environ["THINK_MODE"] = _p2_think

    if args.labels.strip():
        wanted = {s.strip() for s in args.labels.split(",") if s.strip()}
        bad = wanted - set(ALL_LABEL_COLS)
        if bad:
            sys.exit(
                f"[err] --labels: unknown name(s) {sorted(bad)}; "
                f"valid choices: {list(ALL_LABEL_COLS)}"
            )
        if not wanted:
            sys.exit("[err] --labels: empty selection")
        os.environ["ACTIVE_LABELS"] = ",".join(c for c in ALL_LABEL_COLS if c in wanted)
    else:
        os.environ.pop("ACTIVE_LABELS", None)

    args.workers = max(1, min(MAX_WORKERS, args.workers))
    if args.search_gpl is not None:

        return args
    if not args.output:
        sys.exit("[err] --output is required (omit only with --search-gpl)")
    if args.gpl and not args.samples:
        sys.exit("[err] --gpl requires --samples to write into")
    if not args.gpl and not args.samples:
        sys.exit("[err] need --samples (or --gpl + --samples)")
    return args


def _search_gpl(
    query: str, tech: str | None, db_path: Path, limit: int, organism: str | None = None
) -> list[dict]:
    """Mirror of the GUI's _search_platforms_worker. Returns rows sorted
    by sample count (descending). Two SQL passes — first the gpl table,
    then a per-GPL COUNT(*) on gsm to get sample counts."""
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    where, args_sql = [], []
    qu = (query or "").strip().upper()
    is_gpl = qu and ((qu.startswith("GPL") and qu[3:].isdigit()) or qu.isdigit())
    if is_gpl:
        if qu.isdigit():
            qu = f"GPL{qu}"
        where.append("(UPPER(gpl) = ? OR UPPER(gpl) LIKE ?)")
        args_sql += [qu, f"%{qu}%"]
    elif query:
        pat = f"%{query.lower()}%"
        where.append("LOWER(title) LIKE ?")
        args_sql.append(pat)
    if organism:
        where.append("LOWER(organism) LIKE ?")
        args_sql.append(f"%{organism.lower()}%")
    if tech:
        where.append("LOWER(technology) LIKE ?")
        args_sql.append(f"%{tech.lower()}%")
    sql = (
        "SELECT gpl, title, technology, organism, data_row_count "
        "FROM   gpl "
        f"WHERE {' AND '.join(where) if where else '1=1'} "
        f"ORDER BY data_row_count DESC LIMIT {int(max(1 ,limit))}"
    )
    rows = cur.execute(sql, args_sql).fetchall()
    out = [
        {
            "gpl": g,
            "title": t,
            "technology": tc,
            "organism": o,
            "probes": p,
            "samples": None,
        }
        for (g, t, tc, o, p) in rows
    ]

    ids = [r["gpl"] for r in out]
    counts: dict[str, int] = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        ph = ",".join(["?"] * len(chunk))
        try:
            counts.update(
                dict(
                    cur.execute(
                        f"SELECT gpl, COUNT(*) FROM gsm WHERE gpl IN ({ph}) "
                        f"GROUP BY gpl",
                        chunk,
                    ).fetchall()
                )
            )
        except Exception:
            pass
    for r in out:
        r["samples"] = int(counts.get(r["gpl"], 0))
    con.close()
    return out


def _print_gpl_table(rows: list[dict]) -> None:
    if not rows:
        print("(no GPLs matched)")
        return
    head = (
        f"{'GPL':<10} {'samples':>9} {'probes':>10} "
        f"{'organism':<28} {'technology':<32} title"
    )
    print(head)
    print("-" * min(len(head) + 80, 200))
    for r in rows:
        print(
            f"{r['gpl']:<10} {r['samples']:>9,} "
            f"{(r['probes']or 0):>10,} "
            f"{(r['organism']or '')[:28]:<28} "
            f"{(r['technology']or '')[:32]:<32} "
            f"{(r['title']or '')[:90]}"
        )


def _gse_of(row: dict) -> str:
    for k in ("gse", "series_id", "series", "GSE"):
        v = row.get(k)
        if v:
            return str(v).split(",")[0].strip()
    return ""


def _chan(row: dict, base: str) -> str:
    """Merge every channel variant of a GEO field present on the row.

    Channels are discovered from the row rather than enumerated, so a record
    carrying ch1..chN is consumed in full. GEOmetadb materialises ch1 and ch2,
    but a SOFT record or a future source may carry more and the pipeline must
    not silently read only the first two. Bare ``base`` sorts first, then
    channels in numeric order; distinct non-empty values join with ' | '.
    """
    keys = [base] + sorted(
        (
            k
            for k in row
            if k.startswith(f"{base}_ch") and k[len(base) + 3 :].isdigit()
        ),
        key=lambda k: int(k[len(base) + 3 :]),
    )
    vals: list = []
    for k in keys:
        v = row.get(k)
        if v:
            s_ = str(v).strip()
            if s_ and s_ not in vals:
                vals.append(s_)
    return " | ".join(vals)


def _build_raw(row: dict, extended: bool = True) -> dict:
    """Assemble the prompt inputs for one sample.

    ``extended=False`` yields the PRIMARY fields only — title, source,
    characteristics — with treatment_protocol and description blank. That is
    escalation's first pass. The second pass sets ``extended=True`` and is run
    only for labels the first pass could not answer, because unconditional
    extra text costs accuracy on samples whose answer was already present:
    measured +3.2pp worse on Age and +2.0pp worse on Condition, against -5.0pp
    and -4.1pp when the same fields are shown only on a Not Specified.
    """
    return {
        "gsm_title": row.get("title") or "",
        "source_name": _chan(row, "source_name"),
        "characteristics": _chan(row, "characteristics"),
        "treatment_protocol": _chan(row, "treatment_protocol") if extended else "",
        "description": (row.get("description") or "") if extended else "",
    }


def _is_ns(v) -> bool:
    if v is None:
        return True
    s = str(v).strip().lower()
    return s in ("", NS.lower(), "n/a", "na", "none", "null", "unknown")


def _read_checkpoint(path: Path):
    """Return (done_gsms, rows) — the resume unit is the SAMPLE, not the GSE.

    Resuming per GSE means one unextractable sample discards every other sample
    in its series (~50 on this corpus), and a series that is 90% done restarts
    from zero. Keying on GSM lets a partially-completed series contribute what
    it finished and be topped up on the next pass.

    Duplicates are tolerated here: the LAST record for a GSM wins, so a sample
    re-extracted after a failure supersedes any earlier attempt.
    """
    done: dict[str, dict] = {}
    if not path or not path.exists():
        return set(), []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            for s in obj.get("samples") or []:
                g = s.get("gsm")
                if g:
                    done[g] = s
    return set(done), list(done.values())


def _append_checkpoint(path: Path, gse: str, samples: list) -> None:
    if not path:
        return
    line = json.dumps({"gse": gse, "samples": samples}, ensure_ascii=False, default=str)
    with open(path, "a") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def _write_snapshot(out_path: Path, rows: list, n_done: int) -> None:
    if not out_path:
        return
    snap = (
        out_path.with_suffix(out_path.suffix + ".partial.json")
        if out_path.suffix
        else out_path.with_name(out_path.name + ".partial.json")
    )
    tmp = snap.with_suffix(snap.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(
            {"samples_done": n_done, "samples": rows},
            f,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, snap)


def _find_geometadb(arg: Path | None) -> Path:
    if arg and arg.exists():
        return arg
    for p in (
        HERE / "GEOmetadb.sqlite",
        PARENT / "GEOmetadb.sqlite",
        Path.cwd() / "GEOmetadb.sqlite",
    ):
        if p.exists():
            return p
    sys.exit("[err] GEOmetadb.sqlite not found — pass --geometadb")


def _dump_gpl_samples(gpl: str, db_path: Path, out_path: Path) -> int:
    """Read every GSM on a GPL platform from GEOmetadb into a samples.json."""
    print(f"[gpl] {gpl} -> {out_path} (db={db_path})", flush=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.execute(
        "SELECT gsm, title, source_name_ch1, source_name_ch2, "
        "       characteristics_ch1, characteristics_ch2, "
        "       treatment_protocol_ch1, treatment_protocol_ch2, "
        "       description, series_id "
        "FROM   gsm WHERE gpl = ?",
        (gpl,),
    )
    rows = []
    for r in cur:
        d = dict(r)
        d["gpl"] = gpl
        d["gse"] = (d.pop("series_id") or "").split(",")[0].strip()
        rows.append(d)
    con.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"[gpl] wrote {len(rows)} samples", flush=True)
    return len(rows)


import re
from urllib import request as _urlreq
from urllib.error import URLError, HTTPError

_GEO_SOFT_URL = (
    "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"
    "?targ=self&form=text&view=quick&acc={gse}"
)
_GEO_USER_AGENT = (
    "LLM-GEO-Label-Extractor/1.0 " "(github.com/SciSpectator/LLM-GEO-Label-Extractor)"
)
_GEO_TIMEOUT_S = 30
_GEO_RETRY_DELAYS = (1, 2, 5)
_GEO_LINE_RE = re.compile(
    r"^!Series_(title|summary|overall_design)\s*=\s*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)


def scrape_gse_meta(gse: str) -> dict:
    url = _GEO_SOFT_URL.format(gse=gse)
    text, last_err = "", None
    for delay in (0,) + _GEO_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            req = _urlreq.Request(url, headers={"User-Agent": _GEO_USER_AGENT})
            with _urlreq.urlopen(req, timeout=_GEO_TIMEOUT_S) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                break
        except (URLError, HTTPError, TimeoutError, OSError) as e:
            last_err = e
    if not text:
        return {
            "gse_title": "",
            "gse_summary": "",
            "gse_design": "",
            "_error": str(last_err) if last_err else "",
        }
    buckets = {"title": [], "summary": [], "overall_design": []}
    for m in _GEO_LINE_RE.finditer(text):
        v = m.group(2).strip()
        if v:
            buckets[m.group(1).lower()].append(v)
    return {
        "gse_title": "\n".join(buckets["title"]).strip(),
        "gse_summary": "\n".join(buckets["summary"]).strip(),
        "gse_design": "\n".join(buckets["overall_design"]).strip(),
    }


def run_pipeline(args: argparse.Namespace, stop_evt) -> None:

    from phase1 import Phase1Agent
    from phase1b import Phase1bAgent
    from sex_normalize import normalize_sex
    from gse_context_cache import GSEContextCache
    from gse_summarizer import get_or_build_compressed
    from cached_extractors import (
        CachedPhase1Agent,
        CachedPhase1bAgent,
        PHASE1_PROMPT_VERSION,
        PHASE1B_PROMPT_VERSION,
    )

    log = lambda m: print(m, flush=True)

    _active = _active_labels()
    _active_p1b_p2 = _active_label_cols()
    if hasattr(Phase1Agent, "TOOLS"):
        try:
            Phase1Agent.TOOLS = {
                k: v for k, v in Phase1Agent.TOOLS.items() if k in _active
            }
        except Exception as e:
            log(f"[labels] could not filter Phase1Agent.TOOLS: {e!r}")
    log(f"[labels] active={list(_active)}  phase1b/2={list(_active_p1b_p2)}")

    from mesh_lookup import MeshDB as _MeshDB

    try:
        _MeshDB.verify_pipeline_health(
            ollama_url=getattr(args, "ollama_url", "http://127.0.0.1:11434"),
            model_name=args.model,
            gse_cache_db=os.environ.get(
                "GSE_CONTEXT_CACHE", "gse_context_cache.sqlite"
            ),
            require_biolord=not args.no_p2,
            strict=True,
        )
        log("[health] preflight OK (ollama, mesh.sqlite, gse_context_cache, BioLORD)")
    except RuntimeError as _e:
        log(f"[health] PREFLIGHT FAILED: {_e}")
        log(
            "[health] aborting before extraction. Run: "
            "python -c 'from mesh_lookup import MeshDB; MeshDB.repair_oov_mesh()'"
        )
        raise

    _phase_err: dict = {
        "p1": 0,
        "p1b": 0,
        "p2": 0,
        "samples": 0,
        "dropped": 0,
        "p1_retry": 0,
    }
    _PHASE_ERR_FRAC = 0.25

    def _phase_health_assert(phase: str) -> None:
        n = max(1, _phase_err["samples"])
        if _phase_err[phase] / n > _PHASE_ERR_FRAC:
            raise RuntimeError(
                f"[health] phase {phase} error rate "
                f"{_phase_err[phase]}/{n} > {_PHASE_ERR_FRAC :.0%}. "
                f"Likely Ollama wedged, GPU OOM, or DB corrupt. Aborting."
            )

    if args.gpl:
        db = _find_geometadb(args.geometadb)
        _dump_gpl_samples(args.gpl, db, args.samples)

    with open(args.samples) as f:
        all_rows = json.load(f)
    if args.limit > 0:
        all_rows = all_rows[: args.limit]

    resumed_gsms, resumed_rows = set(), []
    if args.checkpoint and not args.no_resume:
        resumed_gsms, resumed_rows = _read_checkpoint(args.checkpoint)

    if resumed_gsms:
        kept = [r for r in all_rows if r.get("gsm") not in resumed_gsms]
        log(
            f"[run] resume — {len(resumed_gsms)} samples already in checkpoint, "
            f"{len(kept)} remaining"
        )
        all_rows = kept

    by_gse: dict[str, list[dict]] = defaultdict(list)
    for r in all_rows:
        by_gse[_gse_of(r)].append(r)
    log(f"[run] {len(all_rows)} samples to extract across {len(by_gse)} GSEs")

    meta_path = HERE / "gse_meta_scraped.json"
    gse_meta: dict[str, dict] = {}
    if meta_path.exists():
        try:
            gse_meta = json.load(open(meta_path))
        except Exception:
            gse_meta = {}
    if not args.no_scrape:
        todo = [g for g in by_gse if g and g not in gse_meta]
        log(f"[scrape] need {len(todo)} GSEs from NCBI")
        for i, gse in enumerate(todo, 1):
            if stop_evt["stop"]:
                break
            gse_meta[gse] = scrape_gse_meta(gse)
            if i % 5 == 0 or i == len(todo):
                json.dump(gse_meta, open(meta_path, "w"), indent=2, sort_keys=True)
            log(f"  [{i}/{len(todo)}] {gse}")
            time.sleep(0.3)
        json.dump(gse_meta, open(meta_path, "w"), indent=2, sort_keys=True)

    cache = GSEContextCache()
    p1_agent = CachedPhase1Agent(
        Phase1Agent(),
        cache,
        model_version=args.model,
        prompt_version=PHASE1_PROMPT_VERSION,
    )
    p1b_agent = CachedPhase1bAgent(
        Phase1bAgent(),
        cache,
        model_version=args.model,
        prompt_version=PHASE1B_PROMPT_VERSION,
    )

    coordinator = None
    if not args.no_p2:
        try:
            from agents import Coordinator
            from mesh_lookup import MeshDB

            coordinator = Coordinator(
                n_collapsers=args.workers,
                n_verifiers=1,
                use_router=False,
                db=MeshDB(),
                cache=cache,
            )
            coordinator.start()
            log(f"[phase2] coordinator started (collapsers={args.workers})")
        except Exception as e:

            raise RuntimeError(
                f"[phase2] init FAILED: {e!r}. Phase 2 was requested but its "
                f"in-process engine (agents.Coordinator + MeshDB) could not start. "
                f"In this package Phase 2 is run as its own stage: pass --no-p2 "
                f"here, then run code/2_normalization_phase2/run_phase2.py."
            ) from e

    out_rows = list(resumed_rows)
    total = len(by_gse)
    import threading

    _ckpt_lock = threading.Lock()
    _out_lock = threading.Lock()

    def _process_one_gse(idx, gse_id, rows):
        if stop_evt["stop"]:
            return None
        gmeta = dict(gse_meta.get(gse_id, {}) or {})
        if gmeta:
            try:
                gmeta["gse_summary"] = get_or_build_compressed(
                    cache,
                    gse_id,
                    gmeta.get("gse_title", ""),
                    gmeta.get("gse_summary", ""),
                    gmeta.get("gse_design", ""),
                    max_chars=512,
                )
            except Exception as e:
                log(f"  [summarize {gse_id}] {e!r}")
        t0 = time.time()

        if not args.no_p1:

            def _do_p1(r):
                attempt, delay = 0, _P1_BACKOFF_START
                while True:
                    try:

                        res = p1_agent.extract(
                            _build_raw(r, extended=False), gsm=r.get("gsm"), gse=gse_id
                        )

                        unresolved = [c for c in _active if _is_ns(res.get(c))]
                        if unresolved:
                            ext = _build_raw(r, extended=True)
                            if ext["treatment_protocol"] or ext["description"]:
                                for c in unresolved:
                                    v = p1_agent.extract_field(
                                        ext, c, gsm=r.get("gsm"), gse=gse_id
                                    )
                                    if not _is_ns(v):
                                        res[c] = v
                        return res
                    except Exception as e:

                        _phase_err["p1_retry"] += 1
                        attempt += 1
                        if attempt >= _P1_MAX_ATTEMPTS:

                            _phase_err["p1"] += 1
                            log(
                                f"  [p1 DROPPED {r.get('gsm')}] after {attempt} "
                                f"attempts: {e!r} — not written, will retry on "
                                f"resume"
                            )
                            return _P1_FAILED
                        log(
                            f"  [p1 retry {attempt}/{_P1_MAX_ATTEMPTS} "
                            f"{r.get('gsm')}] {e!r} — waiting {delay :.0f}s"
                        )
                        time.sleep(delay)
                        delay = min(delay * 2.0, _P1_MAX_BACKOFF)

            p1_results = _pmap(_do_p1, rows, args.workers)

            dropped = sum(1 for x in p1_results if x is _P1_FAILED)
            if dropped:
                _phase_err["dropped"] += dropped
                log(
                    f"  [GSE {gse_id}] {dropped}/{len(rows)} samples dropped "
                    f"(not written) — will retry on resume"
                )
                rows = [r for r, x in zip(rows, p1_results) if x is not _P1_FAILED]
                p1_results = [x for x in p1_results if x is not _P1_FAILED]
                if not rows:
                    return
            for r, res in zip(rows, p1_results):
                r["_phase1"] = res
        else:
            for r in rows:
                r["_phase1"] = {c: NS for c in _active}

        if not args.no_p1b and _active_p1b_p2:
            gse_dist = {c: Counter() for c in _active_p1b_p2}
            for r in rows:
                for c in _active_p1b_p2:
                    gse_dist[c][r["_phase1"].get(c, NS)] += 1

            def _do_p1b(r):
                p1l = dict(r["_phase1"])
                if not any(_is_ns(p1l.get(c, NS)) for c in _active_p1b_p2):
                    return p1l
                sib = {c: Counter(gse_dist[c]) for c in _active_p1b_p2}
                for c in _active_p1b_p2:
                    own = p1l.get(c, NS)
                    if sib[c][own] > 0:
                        sib[c][own] -= 1
                        if sib[c][own] == 0:
                            del sib[c][own]
                try:
                    p1b_out = p1b_agent.infer_sample(
                        r["gsm"], _build_raw(r), p1l, gse_id, gmeta, sibling_dist=sib
                    )
                except Exception as e:
                    log(f"  [p1b err {r.get('gsm')}] {e!r}")
                    _phase_err["p1b"] += 1
                    return p1l

                merged = dict(p1l)
                for c in _active_p1b_p2:
                    if c in p1b_out:
                        merged[c] = p1b_out[c]
                return merged

            p1b_results = _pmap(_do_p1b, rows, args.workers)
            for r, res in zip(rows, p1b_results):
                r["_phase1b"] = res
        else:
            for r in rows:
                r["_phase1b"] = dict(r["_phase1"])

        for r in rows:
            vals = dict(r["_phase1b"])
            if "Sex" in vals:
                vals["Sex"] = normalize_sex(vals["Sex"])[0]
            r["_phase1b"] = vals

        for r in rows:
            for c in _active:
                try:
                    cache.upsert_phase_value(
                        gse_id, r["gsm"], c, "p1b", r["_phase1b"].get(c, NS)
                    )
                except Exception:
                    pass

        if coordinator is not None and _active_p1b_p2:
            ctx = "\n".join(
                [
                    gmeta.get("gse_title", ""),
                    gmeta.get("gse_summary", ""),
                    gmeta.get("gse_design", ""),
                ]
            ).strip()

            try:
                _unique: set = set()
                for r in rows:
                    for c in _active_p1b_p2:
                        raw = (r["_phase1b"].get(c) or "").strip()
                        if raw and not _is_ns(raw):
                            _unique.add((raw, c))
                if _unique:
                    log(
                        f"  [p2-warmup {gse_id}] seeding {len(_unique)} "
                        f"unique (raw,col) into per-GSE sibling cache"
                    )

                    for _raw, _col in sorted(_unique):
                        try:
                            coordinator.collapse(
                                _raw, _col, context=ctx, gse_id=gse_id, timeout=180.0
                            )
                        except Exception as e:
                            log(
                                f"  [p2-warmup err {gse_id}/{_raw!r}/{_col}] "
                                f"{e!r}"
                            )
            except Exception as e:
                log(f"  [p2-warmup err {gse_id}] {e!r}")

            def _do_p2(r):

                p2: dict[str, str] = dict(r["_phase1b"])
                for c in _active_p1b_p2:
                    raw = r["_phase1b"].get(c, NS)
                    if _is_ns(raw):
                        p2[c] = NS
                        continue
                    try:
                        resp = coordinator.collapse(
                            raw, c, context=ctx, gse_id=gse_id, timeout=180.0
                        )
                        p2[c] = resp.get("canonical") or resp.get("label") or raw
                    except Exception as e:
                        log(f"  [p2 err {r.get('gsm')}/{c}] {e!r}")
                        _phase_err["p2"] += 1
                        p2[c] = raw
                return p2

            p2_results = _pmap(_do_p2, rows, args.workers)
            for r, p2 in zip(rows, p2_results):
                r["_phase2"] = p2
        else:
            for r in rows:
                r["_phase2"] = dict(r["_phase1b"])

        dt = time.time() - t0
        gse_samples = [
            {
                "gsm": r.get("gsm"),
                "gse": gse_id,
                "gpl": r.get("gpl"),
                "title": r.get("title"),
                "source": _chan(r, "source_name"),
                "characteristics": _chan(r, "characteristics"),
                "treatment_protocol": _chan(r, "treatment_protocol"),
                "description": r.get("description") or "",
                "phase1": r.get("_phase1"),
                "phase1b": r.get("_phase1b"),
                "phase2": r.get("_phase2"),
            }
            for r in rows
        ]

        with _out_lock:
            prev_done = len(out_rows)
            out_rows.extend(gse_samples)
            do_snap = len(out_rows) // SNAPSHOT_EVERY_N > prev_done // SNAPSHOT_EVERY_N
            n_now = len(out_rows)
        if args.checkpoint:
            with _ckpt_lock:
                _append_checkpoint(args.checkpoint, gse_id, gse_samples)
        if do_snap:
            try:
                _write_snapshot(args.output, out_rows, n_now)
                log(f"  [snapshot] {n_now} samples")
            except Exception as e:
                log(f"  [snapshot err] {e!r}")

        _phase_err["samples"] += len(rows)
        for _ph in ("p1", "p1b", "p2"):
            _phase_health_assert(_ph)

        log(
            f"  [{idx}/{total}] {gse_id} n={len(rows)} {dt :.1f}s "
            f"err[p1/p1b/p2]="
            f"{_phase_err['p1']}/{_phase_err['p1b']}/{_phase_err['p2']}"
        )
        return gse_samples

    try:
        gse_workers = max(1, min(args.workers, args.gse_workers))
        log(f"[run] processing {total} GSEs with gse_workers={gse_workers}")
        if gse_workers <= 1:
            for idx, (gse_id, rows) in enumerate(by_gse.items(), 1):
                if stop_evt["stop"]:
                    log("[run] stopped")
                    break
                _process_one_gse(idx, gse_id, rows)
        else:
            with ThreadPoolExecutor(max_workers=gse_workers) as ex:
                futs = []
                for idx, (gse_id, rows) in enumerate(by_gse.items(), 1):
                    futs.append(ex.submit(_process_one_gse, idx, gse_id, rows))
                for fut in futs:
                    try:
                        fut.result()
                    except Exception as e:
                        log(f"[gse-fut-err] {e!r}")
    finally:
        if coordinator is not None:
            try:
                coordinator.stop()
            except Exception:
                pass
        try:
            cache.close()
        except Exception:
            pass

    _out_path = Path(args.output)
    _tmp = _out_path.with_suffix(_out_path.suffix + ".tmp")
    with open(_tmp, "w") as f:
        json.dump(
            {
                "samples": out_rows,
                "n_samples": len(out_rows),
                "n_gses": len({s.get("gse") for s in out_rows if s.get("gse")}),
            },
            f,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        f.flush()
        os.fsync(f.fileno())
    os.replace(_tmp, _out_path)
    log(f"[run] wrote {args.output} ({len(out_rows)} samples)")


def main() -> None:
    args = _parse_args()

    if args.search_gpl is not None:
        db = _find_geometadb(args.geometadb)
        rows = _search_gpl(
            args.search_gpl, args.tech, db, args.list_gpl_limit, organism=args.organism
        )
        _print_gpl_table(rows)
        sys.exit(0)

    os.environ["LLM_BACKEND"] = args.backend
    os.environ["PHASE1_BACKEND"] = args.backend
    os.environ["PHASE1_MODEL"] = args.model
    os.environ["OLLAMA_URL"] = args.ollama_url
    os.environ["OLLAMA_HOST"] = args.ollama_url
    os.environ["VLLM_URL"] = args.vllm_url
    os.environ["OLLAMA_NUM_PARALLEL"] = str(args.workers)

    for env_name, fname in (
        ("MESH_DB", "mesh.sqlite"),
        ("GSE_CONTEXT_CACHE", "gse_context_cache.sqlite"),
    ):
        if env_name in os.environ:
            continue
        parent_path = PARENT / fname
        local_path = HERE / fname
        os.environ[env_name] = str(
            parent_path
            if (parent_path.exists() and parent_path.stat().st_size > 0)
            else local_path
        )

    stop_evt = {"stop": False}

    def _on_sig(sig, _frame):
        stop_evt["stop"] = True
        print(f"[run] signal {sig} — finishing current GSE then exiting", flush=True)

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    print(
        f"[run] backend={args.backend} workers={args.workers} "
        f"model={args.model}",
        flush=True,
    )
    run_pipeline(args, stop_evt)


if __name__ == "__main__":
    main()
