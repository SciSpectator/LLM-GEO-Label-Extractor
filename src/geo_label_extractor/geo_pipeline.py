#!/usr/bin/env python3
"""End-to-end GEO metadata extraction, normalization and assembly.

The pipeline turns free-text GEO sample metadata into five controlled-vocabulary
fields (Sex, Age, Tissue, Condition, Treatment) in three connected stages:

  Stage 1 - extraction
      A local instruction-tuned model reads each sample's title, source,
      characteristics, treatment protocol and description, and returns verbatim
      field values. Sex and Age are finalized here; Tissue, Condition and
      Treatment are passed on as free text.

  Stage 2 - normalization
      Every distinct Tissue/Condition/Treatment string in the corpus is
      collected into a dictionary and resolved once against MeSH (descriptors,
      entry terms and supplementary concept records) and Cellosaurus. The
      resolved dictionary is then applied to every sample by lookup.

  Stage 3 - assembly
      The per-platform Phase 2 tables are merged into one shipped corpus
      carrying all five final labels, and normalized cells that repeat an
      identifier have the duplicate label relocated out. This is the connected
      form of the final merge the manuscript performed by hand between its
      separately scheduled GPU jobs.

Normalizing the dictionary rather than the samples is what makes the corpus
tractable and internally consistent: repeated values collapse by roughly twenty
fold, and one string cannot receive two different targets in the same run.

Both stages checkpoint per sample and resume from the last completed unit, so an
interrupted run continues rather than restarting.

Usage
-----
    python geo_pipeline.py --input geo_metadata.sqlite --out-dir results
    python geo_pipeline.py --input geo_metadata.sqlite --out-dir results \\
        --from-stage normalize        # reuse existing extraction output

Stage 2 requires an OpenAI-compatible inference server for each GPU replica,
listed in PHASE2_VLLM_URLS.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import subprocess
import sys
import time

from geo_label_extractor.selection import ALL_LABELS, resolve_spec
from geo_label_extractor import final_assembly, metadata_fields

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = ("extract", "normalize", "assemble")


def _log(msg: str) -> None:
    print(f"[pipeline] {msg}", flush=True)


def _stamp(path: str, payload: dict) -> None:
    """Record stage completion so a resumed run can skip finished work."""
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)


def _completed(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _selected_gpls(args: argparse.Namespace) -> list[str]:
    selected = list(args.gpl or [])
    if args.gpl_manifest:
        with open(args.gpl_manifest, encoding="utf-8") as handle:
            selected.extend(line.strip().split(",")[0] for line in handle
                            if line.strip() and not line.lstrip().startswith("#"))
    return sorted({g.upper() for g in selected})


def _manifest(path: str | None, prefix: str) -> list[str]:
    if not path:
        return []
    values = []
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            value = line.strip().split(",")[0].split("\t")[0].upper()
            if value.startswith(prefix) and value[len(prefix):].isdigit():
                values.append(value)
    return values


def _resolve_selection(args: argparse.Namespace) -> None:
    args.gsm = list(args.gsm or []) + _manifest(args.gsm_manifest, "GSM")
    args.gpl = list(args.gpl or []) + _manifest(args.gpl_manifest, "GPL")
    if args.spec:
        selected = resolve_spec(args.input, args.spec, model=args.phase1_model)
        args.gsm.extend(selected.gsms)
        args.gpl.extend(selected.gpls)
        if not args.labels:
            args.labels = ",".join(selected.labels)
        if args.stop_after == "auto":
            args.stop_after = selected.stop_after
        _log(f"selection specification: {selected.explanation}")
    args.gsm = sorted(set(value.upper() for value in args.gsm))
    args.gpl = sorted(set(value.upper() for value in args.gpl))
    if args.stop_after == "auto":
        args.stop_after = "phase2"


def _prepare_samples(args: argparse.Namespace) -> tuple[str, str | None]:
    """Return a run_cli-compatible JSON path and optional GEOmetadb path."""
    source = os.path.abspath(args.input)
    if not source.lower().endswith((".sqlite", ".sqlite3", ".db")):
        return source, None

    target = os.path.join(args.out_dir, "input_samples.json")
    if os.path.exists(target) and not args.force:
        _log(f"reusing materialized sample input {target}")
        return target, source

    gpls = _selected_gpls(args)
    gsms = list(args.gsm or [])
    where = ""
    params: list[str] = []
    connection = sqlite3.connect(source)
    clauses: list[str] = []
    if gpls or gsms:
        connection.execute("CREATE TEMP TABLE selected_gpl (id TEXT PRIMARY KEY)")
        connection.execute("CREATE TEMP TABLE selected_gsm (id TEXT PRIMARY KEY)")
        connection.executemany("INSERT OR IGNORE INTO selected_gpl VALUES (?)",
                               ((value,) for value in gpls))
        connection.executemany("INSERT OR IGNORE INTO selected_gsm VALUES (?)",
                               ((value,) for value in gsms))
        clauses.append("(gpl IN (SELECT id FROM selected_gpl) "
                       "OR gsm IN (SELECT id FROM selected_gsm))")
    # Modality and organism are properties of the platform, so they narrow the
    # sample set through the gpl catalogue. GEO's technology strings are e.g.
    # "high-throughput sequencing" for RNA-seq and single-cell work and
    # "in situ oligonucleotide" for arrays; the match is a substring so a
    # partial term still selects. Nothing downstream is aware of the choice:
    # every platform's records carry the same text fields the extractor reads.
    if getattr(args, "tech", ""):
        clauses.append("gpl IN (SELECT gpl FROM gpl WHERE technology LIKE ?)")
        params.append(f"%{args.tech}%")
    if getattr(args, "organism", ""):
        clauses.append("gpl IN (SELECT gpl FROM gpl WHERE organism LIKE ?)")
        params.append(f"%{args.organism}%")
    if clauses:
        where = " WHERE " + " AND ".join(clauses)
    # The base projection is what the published run materialized. Every column
    # in it is optional, because an input need not be GEOmetadb: a single-channel
    # source such as ARCHS4 has no _ch2 columns at all -- two channels are an
    # array notion, and RNA-seq has one -- and asking for them turns a perfectly
    # good input into "no such column: source_name_ch2".
    base_wanted = ["gsm", "gpl", "series_id", "title", "source_name_ch1",
                   "source_name_ch2", "characteristics_ch1", "characteristics_ch2",
                   "treatment_protocol_ch1", "treatment_protocol_ch2", "description"]
    have = {row[1] for row in connection.execute("PRAGMA table_info(gsm)")}
    for required in ("gsm", "series_id"):
        if required not in have:
            raise SystemExit(
                f"input has no {required!r} column; it cannot identify samples")
    base = [c for c in base_wanted if c in have]
    skipped = [c for c in base_wanted if c not in have]
    if skipped:
        _log("input does not carry: " + ", ".join(skipped))

    chosen = metadata_fields.parse(getattr(args, "fields", ""))
    missing = [c for c in chosen if c not in have]
    if missing:
        raise SystemExit(
            f"--fields names columns this input does not have: "
            f"{', '.join(missing)}\navailable: "
            f"{', '.join(sorted(metadata_fields.available(source)))}"
        )
    extra = [c for c in chosen if c not in base]
    if extra:
        _log("extra metadata columns for the model: " + ", ".join(extra))
    sql = ("SELECT " + ", ".join(base + extra) + " FROM gsm"
           + where + " ORDER BY gsm")
    scope = []
    if gpls or gsms:
        scope.append(f"{len(gpls)} GPLs and {len(gsms)} GSMs")
    if getattr(args, "tech", ""):
        scope.append(f"technology like {args.tech!r}")
    if getattr(args, "organism", ""):
        scope.append(f"organism like {args.organism!r}")
    _log("materializing GEOmetadb rows " +
         ("for " + ", ".join(scope) if scope else "(every platform)"))
    connection.row_factory = sqlite3.Row
    count = 0
    with open(target + ".tmp", "w", encoding="utf-8") as handle:
        handle.write("[")
        for row in connection.execute(sql, params):
            record = dict(row)
            record["gse"] = str(record.pop("series_id") or "").split(",")[0].strip()
            if count:
                handle.write(",")
            json.dump(record, handle, ensure_ascii=False)
            count += 1
            if args.limit and count >= args.limit:
                break
        handle.write("]")

    meta_target = os.path.join(args.out_dir, "gse_meta.json")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(gse)")}
    if "gse" in columns:
        title_col = "title" if "title" in columns else "''"
        summary_col = "summary" if "summary" in columns else "''"
        design_col = ("overall_design" if "overall_design" in columns else
                      ("design" if "design" in columns else "''"))
        context = {}
        query = (f"SELECT gse, {title_col}, {summary_col}, {design_col} "
                 "FROM gse")
        for gse, title, summary, design in connection.execute(query):
            context[str(gse)] = {"gse_title": title or "",
                                 "gse_summary": summary or "",
                                 "gse_design": design or ""}
        with open(meta_target + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(context, handle, ensure_ascii=False)
        os.replace(meta_target + ".tmp", meta_target)
    connection.close()
    os.replace(target + ".tmp", target)
    _log(f"materialized {count:,} samples")
    return target, source


def run_extraction(args: argparse.Namespace) -> list[str]:
    """Stage 1. Returns the shard files holding extracted samples."""
    marker = os.path.join(args.out_dir, "extract.done")
    single = os.path.join(args.out_dir, "extracted.json")
    shards = sorted(glob.glob(os.path.join(args.out_dir, "extracted_*.json")))
    if os.path.exists(single):
        shards.insert(0, single)
    done = _completed(marker)
    if done and shards:
        _log(f"extraction already complete: {done['n_samples']:,} samples "
             f"in {len(shards)} shards")
        return shards

    samples, geometadb = _prepare_samples(args)
    os.environ.setdefault("GSE_CONTEXT_CACHE", os.path.join(
        args.out_dir, "gse_context_cache.sqlite"))
    os.environ.setdefault("GSE_META_CACHE", os.path.join(
        args.out_dir, "gse_meta.json"))
    _log(f"extraction starting from {samples}")
    t0 = time.time()
    cmd = [
        sys.executable, os.path.join(HERE, "run_cli.py"),
        "--samples", samples,
        "--output", os.path.join(args.out_dir, "extracted.json"),
        "--checkpoint", os.path.join(args.out_dir, "extract.ckpt"),
        "--workers", str(args.extract_workers),
        "--backend", args.backend,
        "--model", args.phase1_model,
        "--vllm-url", args.vllm_url,
        "--no-p2",
    ]
    if getattr(args, "no_scrape", False):
        cmd.append("--no-scrape")
    if args.stop_after == "phase1":
        cmd.append("--no-p1b")
    if args.labels:
        cmd += ["--labels", args.labels]
    if geometadb:
        cmd += ["--geometadb", geometadb]
    if args.tech:
        cmd += ["--tech", args.tech]
    if args.organism:
        cmd += ["--organism", args.organism]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    rc = subprocess.call(cmd)
    if rc != 0:
        raise SystemExit(f"extraction failed with exit code {rc}")

    shards = sorted(glob.glob(os.path.join(args.out_dir, "extracted_*.json")))
    if os.path.exists(single):
        shards.insert(0, single)
    n = 0
    for p in shards:
        with open(p) as fh:
            d = json.load(fh)
        n += len(d["samples"] if isinstance(d, dict) and "samples" in d else d)
    _stamp(marker, {"n_samples": n, "shards": shards,
                    "seconds": round(time.time() - t0)})
    _log(f"extraction complete: {n:,} samples in {time.time()-t0:.0f}s")
    return shards


def run_normalization(args: argparse.Namespace, shards: list[str]) -> int:
    """Stage 2. Resolves the label dictionary and applies it to every sample."""
    marker = os.path.join(args.out_dir, "normalize.done")
    if _completed(marker) and not args.force:
        _log("normalization already complete")
        return 0

    if not shards:
        raise SystemExit("no extraction output found; run stage 1 first")

    pattern = (shards[0] if len(shards) == 1 else
               os.path.join(os.path.dirname(shards[0]), "extracted*.json"))
    _log(f"normalization starting over {len(shards)} shards")
    t0 = time.time()
    cmd = [
        sys.executable, os.path.join(HERE, "run_phase2.py"),
        "--corpus", pattern,
        "--out-dir", os.path.join(args.out_dir, "normalized"),
        "--workers", str(args.normalize_workers),
        "--vocab", args.vocab,
        "--index", args.index,
        "--cellosaurus", args.cellosaurus,
    ]
    rc = subprocess.call(cmd)
    if rc != 0:
        raise SystemExit(f"normalization failed with exit code {rc}")

    # The cascade mints one out-of-vocabulary concept per surface form, so the
    # same concept can end up on ids differing only in case, whitespace or
    # hyphenation (PBMC/pbmc, wild type/wildtype). Folding them is the first of
    # the two deterministic steps that close Phase 2; it touches OOV ids only.
    normalized = os.path.join(args.out_dir, "normalized")
    _log("folding out-of-vocabulary surface variants")
    rc = subprocess.call([sys.executable,
                          os.path.join(HERE, "phase2_casefold.py"), normalized])
    if rc != 0:
        raise SystemExit(f"case-fold failed with exit code {rc}")

    _stamp(marker, {"seconds": round(time.time() - t0), "corpus": pattern})
    _log(f"normalization complete in {time.time()-t0:.0f}s")
    return 0


def run_assembly(args: argparse.Namespace) -> int:
    """Stage 3. Merge per-platform tables and relocate duplicate-id labels."""
    marker = os.path.join(args.out_dir, "assemble.done")
    if _completed(marker) and not args.force:
        _log("assembly already complete")
        return 0

    final_dir = os.path.join(args.out_dir, "normalized", "final")
    if not os.path.isdir(final_dir):
        raise SystemExit(
            f"no Phase 2 export at {final_dir}; run normalization first")

    out_dir = os.path.join(args.out_dir, "final_labels")
    _log(f"assembling final corpus from {final_dir}")
    t0 = time.time()
    manifest = final_assembly.assemble(final_dir, out_dir, force=args.force)

    # Phase 1 extracts one or more spans per field and Phase 2 normalises each
    # separately, so two spans of one sample that denote the same concept leave
    # a repeated id in its multi-label cell. Collapsing those is the second
    # deterministic step closing Phase 2; distinct ids are left alone.
    corpus = os.path.join(out_dir, "LLM_labels_all_samples.csv.gz")
    if os.path.exists(corpus):
        _log("de-duplicating repeated ids within samples")
        deduped = corpus + ".dedup"
        rc = subprocess.call([sys.executable,
                              os.path.join(HERE, "phase2_dedup.py"),
                              corpus, deduped])
        if rc != 0:
            raise SystemExit(f"de-duplication failed with exit code {rc}")
        os.replace(deduped, corpus)

    _stamp(marker, {"seconds": round(time.time() - t0), **manifest})
    _log(f"assembly complete in {time.time()-t0:.0f}s: "
         f"{manifest['samples']:,} samples over {manifest['platforms']:,} "
         f"platforms -> {out_dir}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="GEO metadata extraction and normalization pipeline")
    p.add_argument("--input", required=True,
                   help="GEOmetadb SQLite database or run_cli-compatible JSON")
    p.add_argument("--out-dir", required=True,
                   help="directory for shards, checkpoints and final corpus")
    p.add_argument("--from-stage", choices=STAGES, default="extract",
                   help="stage to start from; earlier stages must have completed")
    p.add_argument("--limit", type=int, default=0,
                   help="process only the first N samples (development runs)")
    p.add_argument("--gpl", action="append", default=[],
                   help="restrict SQLite input to a GPL; repeat as needed")
    p.add_argument("--gpl-manifest",
                   help="newline/CSV-first-column GPL accession manifest")
    p.add_argument("--gsm", action="append", default=[],
                   help="select one GSM accession; repeat as needed")
    p.add_argument("--gsm-manifest",
                   help="newline/CSV-first-column GSM accession manifest")
    p.add_argument("--spec", default="",
                   help="plain-language GEO request interpreted by the configured LLM")
    # GEO holds every assay type, and the pipeline reads only the text fields a
    # sample record carries, which exist whatever the platform measured. These
    # two narrow which samples are selected; nothing downstream changes.
    p.add_argument("--tech", default="",
                   help="platform technology to select, e.g. 'high-throughput "
                        "sequencing' for RNA-seq or 'in situ oligonucleotide' "
                        "for arrays; omit to accept every modality")
    p.add_argument("--organism", default="",
                   help="organism to select, e.g. 'Homo sapiens'; omit for all")
    p.add_argument("--no-scrape", action="store_true",
                   help="never reach out to NCBI for series metadata missing "
                        "from --input; needed on compute nodes without outbound "
                        "network, where the fetch hangs instead of failing")
    p.add_argument("--fields", default="",
                   help="comma-separated metadata columns the model reads; "
                        "omit for the published set (" +
                        ",".join(metadata_fields.DEFAULT_COLUMNS) + "). "
                        "Use --list-fields to see what an input offers")
    p.add_argument("--list-fields", action="store_true",
                   help="print the columns available in --input and exit")
    p.add_argument("--labels", default="",
                   help=f"comma-separated subset of {','.join(ALL_LABELS)}")
    p.add_argument("--stop-after", choices=("auto", "phase1", "phase1b", "phase2"),
                   default="auto", help="choose extraction depth")
    p.add_argument("--backend", default=os.environ.get("LLM_BACKEND", "vllm"),
                   choices=("vllm", "sglang", "openai"))
    p.add_argument("--llm-url", "--vllm-url", dest="vllm_url", default=os.environ.get(
        "VLLM_URL", "http://127.0.0.1:8000/v1"))
    p.add_argument("--phase1-model", default=os.environ.get(
        "PHASE1_MODEL", "google/gemma-4-12b-it"))
    p.add_argument("--age-model", default=os.environ.get(
        "AGE_MODEL", "google/gemma-4-e2b-it"))
    p.add_argument("--phase2-model", default=os.environ.get(
        "PHASE2_MODEL", "google/gemma-4-e2b-it"))
    p.add_argument("--vocab", default="",
                   help="MeSH vocabulary SQLite built by build_vocab.py")
    p.add_argument("--index", default="",
                   help="BioLORD retrieval index built by build_index.py")
    p.add_argument("--cellosaurus", default="",
                   help="Cellosaurus SQLite built by build_cellosaurus_db.py")
    p.add_argument("--extract-workers", type=int, default=64)
    p.add_argument("--normalize-workers", type=int, default=512)
    p.add_argument("--force", action="store_true",
                   help="re-run a stage whose completion marker already exists")
    p.add_argument("--no-assemble", action="store_true",
                   help="stop after normalization; skip the final merge and "
                        "relocation stage")
    args = p.parse_args()

    if args.list_fields:
        columns = metadata_fields.available(args.input)
        default = set(metadata_fields.DEFAULT_COLUMNS)
        print(f"metadata columns in {args.input}:")
        for column in columns:
            mark = " (default)" if column in default else ""
            print(f"  {column}{mark}    {metadata_fields.describe(column).description}")
        return 0

    os.environ["LLM_BACKEND"] = args.backend
    os.environ["VLLM_URL"] = args.vllm_url
    # Every worker stage resolves its own fields, so the choice travels by
    # environment rather than being threaded through each call signature.
    chosen_fields = metadata_fields.parse(args.fields)
    metadata_fields.publish(chosen_fields)
    if not metadata_fields.is_default(chosen_fields):
        _log("NON-DEFAULT metadata fields — results are not comparable with "
             "the published run: " + ", ".join(chosen_fields))
    _resolve_selection(args)

    if args.labels:
        labels = [value.strip() for value in args.labels.split(",") if value.strip()]
        unknown = sorted(set(labels) - set(ALL_LABELS))
        if unknown:
            p.error(f"unknown labels: {unknown}; choose from {list(ALL_LABELS)}")

    required_paths = [(args.input, "input")]
    if args.stop_after == "phase2":
        required_paths += [(args.vocab, "vocabulary"),
                           (args.index, "semantic retrieval index"),
                           (args.cellosaurus, "Cellosaurus database")]
    for path, label in required_paths:
        if not os.path.exists(path):
            p.error(f"{label} does not exist: {path}")

    os.environ["LLM_BACKEND"] = args.backend
    os.environ["VLLM_URL"] = args.vllm_url
    os.environ["PHASE1_MODEL"] = args.phase1_model
    os.environ["AGE_MODEL"] = args.age_model
    os.environ["PHASE2_MODEL"] = args.phase2_model

    os.makedirs(args.out_dir, exist_ok=True)
    start = STAGES.index(args.from_stage)

    if start <= STAGES.index("normalize"):
        if start <= STAGES.index("extract"):
            shards = run_extraction(args)
        else:
            shards = sorted(glob.glob(os.path.join(args.out_dir,
                                                   "extracted*.json")))
            _log(f"reusing {len(shards)} extraction shards")

        if args.stop_after in ("phase1", "phase1b"):
            _log(f"stopped after {args.stop_after} as requested")
            return 0

        rc = run_normalization(args, shards)
        if rc != 0:
            return rc

    if args.no_assemble:
        _log("stopped after normalization (--no-assemble)")
        return 0
    return run_assembly(args)


if __name__ == "__main__":
    sys.exit(main())
