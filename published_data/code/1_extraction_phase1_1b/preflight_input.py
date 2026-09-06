"""Assert an extraction input is actually populated before any GPU time is spent.

The failure this exists to prevent: a shard file in which `treatment_protocol`
and `description` are present as KEYS but empty as VALUES. A schema check passes
that file. The model then reads two blank fields for every sample and the loss is
invisible until the corpus is judged.

So every check here is on populated rates, measured against the metadata source
of record, and the prompt is rendered end-to-end and searched for the field text.

Usage:
    preflight_input.py --input shard.json [shard2.json ...] \
                       --meta geo_metadata.sqlite [--label Treatment]

Exit status is non-zero if any check FAILs, so it can gate a launch script.
"""

from __future__ import annotations
import argparse, json, os, random, sqlite3, sys

FIELDS = (
    "title",
    "source_name_ch1",
    "characteristics_ch1",
    "treatment_protocol_ch1",
    "description",
)
META_OF = {
    "title": "title",
    "source_name_ch1": "source_name",
    "characteristics_ch1": "characteristics",
    "treatment_protocol_ch1": "treatment_protocol",
    "description": "description",
}


TOLERANCE = 0.15

results: list[tuple[str, str, str]] = []


def record(status, check, detail):
    results.append((status, check, detail))
    print(f"[{status :<4}] {check}: {detail}", flush=True)


def load(paths):
    rows = []
    for p in paths:
        with open(p) as fh:
            data = json.load(fh)
        rows.extend(data if isinstance(data, list) else [data])
    return rows


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--input", nargs="+", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--label", default="Treatment")
    ap.add_argument("--sample", type=int, default=200)
    a = ap.parse_args()

    rows = load(a.input)
    record("PASS" if rows else "FAIL", "input loads", f"{len(rows):,} rows")
    if not rows:
        return 1

    gsms = [r.get("gsm") for r in rows]
    blank = sum(1 for g in gsms if not g)
    dupes = len(gsms) - len(set(gsms))
    record(
        "FAIL" if blank or dupes else "PASS",
        "identifiers",
        f"{blank :,} blank, {dupes :,} duplicated",
    )

    pop = {f: sum(1 for r in rows if str(r.get(f) or "").strip()) for f in FIELDS}
    for f in FIELDS:
        pct = 100 * pop[f] / len(rows)
        status = "FAIL" if pop[f] == 0 else "PASS"
        record(
            status, f"populated: {f}", f"{pop[f]:,}/{len(rows):,} ({pct :.1f}%)"
        )

    if os.path.exists(a.meta):
        db = sqlite3.connect(a.meta)
        db.execute("CREATE TEMP TABLE t(gsm TEXT PRIMARY KEY)")
        db.executemany("INSERT OR IGNORE INTO t VALUES(?)", [(g,) for g in gsms if g])
        n = db.execute(
            "SELECT COUNT(*) FROM sample s JOIN t ON t.gsm=s.gsm"
        ).fetchone()[0]
        record(
            "WARN" if n < len(set(gsms)) else "PASS",
            "metadata coverage",
            f"{n :,} of {len(set(gsms)):,} inputs found in source",
        )
        for f in FIELDS:
            col = META_OF[f]
            avail = db.execute(
                f"SELECT COUNT(*) FROM sample s JOIN t ON t.gsm=s.gsm "
                f"WHERE COALESCE(s.{col},'') <> ''"
            ).fetchone()[0]
            if not avail:
                continue
            got, exp = pop[f] / len(rows), avail / max(n, 1)
            status = "FAIL" if got < exp - TOLERANCE else "PASS"
            record(
                status,
                f"vs source: {f}",
                f"input {100 * got :.1f}% vs source {100 * exp :.1f}%"
                + ("  <-- DATA LOST IN INPUT BUILD" if status == "FAIL" else ""),
            )
    else:
        record("WARN", "metadata source", f"{a.meta} not found; skipped")

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import phase1

        sig = getattr(phase1, f"_SIG_{a.label.upper()}")
        prm = getattr(phase1, f"_PROMPT_{a.label.upper()}")
        with_text = [
            r for r in rows if str(r.get("treatment_protocol_ch1") or "").strip()
        ]
        pick = random.Random(0).sample(with_text, min(a.sample, len(with_text)))
        missing = 0
        for r in pick:
            vals = {
                "title": r.get("title", ""),
                "source": r.get("source_name_ch1", ""),
                "characteristics": r.get("characteristics_ch1", ""),
                "treatment_protocol": r.get("treatment_protocol_ch1", ""),
                "description": r.get("description", ""),
            }
            rendered = "\n".join(
                m["content"] for m in phase1._build_messages(sig, prm, vals)
            )
            probe = str(r["treatment_protocol_ch1"]).strip()[:40]
            if probe and probe not in rendered:
                missing += 1
        if not pick:
            record(
                "FAIL",
                "prompt carries field text",
                "no input row has treatment_protocol to probe",
            )
        else:
            record(
                "FAIL" if missing else "PASS",
                "prompt carries field text",
                f"{len(pick)-missing}/{len(pick)} rendered prompts contain "
                f"the sample's treatment_protocol",
            )
    except Exception as exc:
        record("WARN", "prompt render", f"{type(exc).__name__}: {exc}")

    fails = sum(1 for s, _, _ in results if s == "FAIL")
    warns = sum(1 for s, _, _ in results if s == "WARN")
    print(
        f"\n{fails} FAIL, {warns} WARN, "
        f"{sum(1 for s ,_ ,_ in results if s =='PASS')} PASS"
    )
    if fails:
        print("REFUSING TO LAUNCH — fix the input build first.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
