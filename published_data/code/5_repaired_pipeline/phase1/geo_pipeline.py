"""End-to-end GEO metadata extraction and normalization.

The pipeline turns free-text GEO sample metadata into five controlled-vocabulary
fields (Sex, Age, Tissue, Condition, Treatment) in two stages:

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

Normalizing the dictionary rather than the samples is what makes the corpus
tractable and internally consistent: repeated values collapse by roughly twenty
fold, and one string cannot receive two different targets in the same run.

Both stages checkpoint per sample and resume from the last completed unit, so an
interrupted run continues rather than restarting.

Usage
-----
    python geo_pipeline.py --input samples_804k.json --out-dir results
    python geo_pipeline.py --input samples_804k.json --out-dir results \\
        --from-stage normalize        # reuse existing extraction output

Stage 2 requires an OpenAI-compatible inference server for each GPU replica,
listed in PHASE2_VLLM_URLS.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = ("extract", "normalize")


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


def run_extraction(args: argparse.Namespace) -> list[str]:
    """Stage 1. Returns the shard files holding extracted samples."""
    marker = os.path.join(args.out_dir, "extract.done")
    shards = sorted(glob.glob(os.path.join(args.out_dir, "extracted_*.json")))
    done = _completed(marker)
    if done and shards:
        _log(
            f"extraction already complete: {done['n_samples']:,} samples "
            f"in {len(shards)} shards"
        )
        return shards

    _log(f"extraction starting from {args.input}")
    t0 = time.time()
    cmd = [
        sys.executable,
        os.path.join(HERE, "run_cli.py"),
        "--samples",
        args.input,
        "--output",
        os.path.join(args.out_dir, "extracted_0.json"),
        "--checkpoint",
        os.path.join(args.out_dir, "extract.ckpt"),
        "--workers",
        str(args.extract_workers),
        "--backend",
        args.backend,
        "--model",
        args.model,
        "--no-p2",
    ]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    rc = subprocess.call(cmd)
    if rc != 0:
        raise SystemExit(f"extraction failed with exit code {rc}")

    shards = sorted(glob.glob(os.path.join(args.out_dir, "extracted_*.json")))
    n = 0
    for p in shards:
        with open(p) as fh:
            d = json.load(fh)
        n += len(d["samples"] if isinstance(d, dict) and "samples" in d else d)
    _stamp(
        marker, {"n_samples": n, "shards": shards, "seconds": round(time.time() - t0)}
    )
    _log(f"extraction complete: {n :,} samples in {time.time()-t0 :.0f}s")
    return shards


def run_normalization(args: argparse.Namespace, shards: list[str]) -> int:
    """Stage 2. Resolves the label dictionary and applies it to every sample."""
    marker = os.path.join(args.out_dir, "normalize.done")
    if _completed(marker) and not args.force:
        _log("normalization already complete")
        return 0

    if not shards:
        raise SystemExit("no extraction output found; run stage 1 first")

    pattern = os.path.join(
        os.path.dirname(shards[0]),
        os.path.basename(shards[0]).rsplit("_", 1)[0] + "_*.json",
    )
    _log(f"normalization starting over {len(shards)} shards")
    t0 = time.time()
    cmd = [
        sys.executable,
        os.path.join(HERE, "run_phase2.py"),
        "--corpus",
        pattern,
        "--out-dir",
        os.path.join(args.out_dir, "normalized"),
        "--workers",
        str(args.normalize_workers),
    ]
    rc = subprocess.call(cmd)
    if rc != 0:
        raise SystemExit(f"normalization failed with exit code {rc}")
    _stamp(marker, {"seconds": round(time.time() - t0), "corpus": pattern})
    _log(f"normalization complete in {time.time()-t0 :.0f}s")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="GEO metadata extraction and normalization pipeline"
    )
    p.add_argument(
        "--input", required=True, help="frozen samples JSON used by the publication run"
    )
    p.add_argument(
        "--out-dir",
        required=True,
        help="directory for shards, checkpoints and final corpus",
    )
    p.add_argument(
        "--from-stage",
        choices=STAGES,
        default="extract",
        help="stage to start from; earlier stages must have completed",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="process only the first N samples (development runs)",
    )
    p.add_argument("--backend", default="vllm")
    p.add_argument("--model", default="google/gemma-4-12B-it")
    p.add_argument("--extract-workers", type=int, default=64)
    p.add_argument("--normalize-workers", type=int, default=512)
    p.add_argument(
        "--force",
        action="store_true",
        help="re-run a stage whose completion marker already exists",
    )
    args = p.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    start = STAGES.index(args.from_stage)

    if start <= STAGES.index("extract"):
        shards = run_extraction(args)
    else:
        shards = sorted(glob.glob(os.path.join(args.out_dir, "extracted_*.json")))
        _log(f"reusing {len(shards)} extraction shards")

    return run_normalization(args, shards)


if __name__ == "__main__":
    sys.exit(main())
