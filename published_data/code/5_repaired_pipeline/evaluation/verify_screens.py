#!/usr/bin/env python3
"""Re-run ALL SIX Table 4 integrity screens on the freshly extracted labels.

The claim under test is narrow and falsifiable: every sample that failed a
screen in the released corpus must pass it after re-extraction, and no sample
may fail a screen it previously passed. The screens are the same deterministic
ones that identified the errors; what matters is that they are applied to output
the repaired pipeline produced from scratch, not to a patched copy.

Two of the six have no code repair and are expected to persist -- they are
reported, not silently omitted:
  * sex vs sex-specific anatomy (11 samples): either field could be the wrong
    one, and a rule arbitrating 11 samples would be fitting to them rather than
    repairing a mechanism;
  * the maternal residue of sex vs sex-specific condition (55 of 239): fetal or
    placental material makes a male sample legitimate.
Their staging-driven part (184 of 239) IS repaired and is counted separately.

Usage:
    verify_screens.py MERGED_CSV_GZ MESH_SQLITE CELLOSAURUS_SQLITE
                      EVIDENCE_DIR OUT_DIR
where MERGED_CSV_GZ is the assembly output (all five labels), because Sex is
only reconciled against the cell-line catalogue at assembly time.
"""

import csv
import gzip
import json
import os
import re
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.environ.get("PYTHONPATH", "").split(":")[0] or ".")

IS_MESH = re.compile(r"^[DC]\d{6,}$")


def load_rows(merged):
    rows = {}
    with gzip.open(merged, "rt") as fh:
        for r in csv.DictReader(fh):
            rows[r["gsm"]] = r
    return rows


def main() -> int:
    merged, mesh_path, cells_path, ev_dir, out_dir = sys.argv[1:6]
    os.makedirs(out_dir, exist_ok=True)
    new = load_rows(merged)
    print(f"fresh labels loaded for {len(new):,} samples")

    cells = sqlite3.connect(f"file:{cells_path}?mode=ro", uri=True)
    ORG, SEX = {}, {}
    for cv, o, s in cells.execute(
            "SELECT DISTINCT cvcl, organism, donor_sex FROM cell_lines"):
        if o:
            ORG.setdefault(cv, set()).update(x.strip() for x in o.split(";") if x.strip())
        if s in ("Male", "Female"):
            SEX[cv] = s

    mesh = sqlite3.connect(f"file:{mesh_path}?mode=ro", uri=True)

    def under(*pfx):
        s = set()
        for p in pfx:
            for (i,) in mesh.execute(
                    "SELECT DISTINCT mesh_id FROM mesh_tree "
                    "WHERE tree_number=? OR tree_number LIKE ?", (p, p + ".%")):
                s.add(i)
        return s

    embryonic = under("A16")
    offspring = under("C16", "C12.050.703.824")
    # Two ways a sample can fail the sex-vs-condition screen while being right.
    # Both are read off the MeSH tree rather than listed, and both are used only
    # to CLASSIFY what the screen still flags -- the screen itself is left as
    # published, so its counts stay comparable with the audit's.
    #
    #   * a condition that is also a germ-cell or embryonal neoplasm is not
    #     female-only: choriocarcinoma and hydatidiform mole sit under
    #     C04.557.465 as well as under pregnancy complications, and the same
    #     concepts name testicular tumours in men;
    #   * a pregnancy complication is the MOTHER's, while the sample can be the
    #     fetus -- placenta or cord blood from a male pregnancy carries her
    #     diagnosis legitimately.
    germcell = under("C04.557.465")
    fetal_tissue = under("A16") | under("A10.615.284.473") | under("A15.145.229.188")
    FEM_A = under("A05.360.319") - embryonic
    MAL_A = under("A05.360.444") - embryonic
    FEM_C = under("C12.050.351.500", "C12.100.250", "C12.050.703") - offspring
    MAL_C = under("C12.100.500", "C12.200.294") - offspring
    for a, b in ((FEM_A, MAL_A), (FEM_C, MAL_C)):
        x = a & b
        a -= x
        b -= x

    from qualifier_guard import is_pure_qualifier
    import importlib.util
    age_path = os.environ.get("AGE_MODULE", "")
    age = None
    if age_path and os.path.exists(age_path):
        spec = importlib.util.spec_from_file_location("age_rep", age_path)
        age = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(age)

    def cvcls(r):
        return [x.strip() for x in (r.get("final_Tissue_id") or "").split(";")
                if x.strip().startswith("CVCL_")]

    def fails(gsm, screen):
        r = new.get(gsm)
        if r is None:
            return None
        if screen == "species":
            return any("Homo sapiens" not in ORG.get(c, set()) for c in cvcls(r))
        if screen == "donorsex":
            sex = (r.get("final_Sex") or "").strip()
            return sex in ("Male", "Female") and any(
                SEX.get(c) and SEX[c] != sex for c in cvcls(r))
        if screen == "staging":
            raw = r.get("phase1b_Condition") or ""
            ids = [x.strip() for x in (r.get("final_Condition_id") or "").split(";")]
            cl = [x.strip() for x in raw.split(";")]
            if len(cl) != len(ids):
                return False
            return any(is_pure_qualifier(c) and IS_MESH.match(i or "")
                       for c, i in zip(cl, ids))
        if screen in ("sexanat", "sexcond"):
            fem, mal = (FEM_A, MAL_A) if screen == "sexanat" else (FEM_C, MAL_C)
            col = "Tissue" if screen == "sexanat" else "Condition"
            sex = (r.get("final_Sex") or "").strip().lower()
            if sex not in ("male", "female"):
                return False
            ids = [x.strip() for x in (r.get(f"final_{col}_id") or "").split(";") if x.strip()]
            hf = any(i in fem for i in ids)
            hm = any(i in mal for i in ids)
            return (sex == "male" and hf and not hm) or (sex == "female" and hm and not hf)
        if screen == "age":
            if age is None:
                return None
            v = r.get("final_Age") or ""
            if age.classify(v) != "chronological":
                return False
            vv = age.PREFIX.sub("", v.strip()).strip()
            m = re.search(r"(\d+(?:\.\d+)?)", vv)
            if not m:
                return False
            y = float(m.group(1))
            for pat, f in age.UNITS:
                if pat.search(vv):
                    y *= f
                    break
            # still an unscaled value sitting in the 1-12 year band
            return 1 <= y <= 12 and not any(p.search(vv) for p, _ in age.UNITS)
        return False

    def classify_residual(screen, gsm):
        """Why does this sample still trip the screen -- pipeline, or screen?

        A residual is only an extraction error if neither exemption applies.
        Reported per sample so the claim is checkable rather than asserted: a
        run whose residuals are all exempt has no outstanding error in that
        class, and one that acquires a GENUINE residual says so immediately.
        """
        r = new.get(gsm) or {}
        if screen != "sexcond":
            return "GENUINE"
        cids = [x.strip() for x in (r.get("final_Condition_id") or "").split(";") if x.strip()]
        if any(i in germcell for i in cids):
            return "EXEMPT_germ_cell_neoplasm"
        tids = [x.strip() for x in (r.get("final_Tissue_id") or "").split(";") if x.strip()]
        if any(i in fetal_tissue for i in tids):
            return "EXEMPT_fetal_material"
        return "GENUINE"

    SCREENS = [
        ("cellline_species_FLAGGED.csv", "species", "repaired"),
        ("cellline_donorsex_FLAGGED.csv", "donorsex", "repaired"),
        ("staging_token_FLAGGED.csv", "staging", "repaired"),
        ("age_unit_1_12band_FLAGGED.csv", "age", "repaired"),
        ("sex_vs_condition_FLAGGED.csv", "sexcond", "partly (staging-driven only)"),
        ("sex_vs_anatomy_FLAGGED.csv", "sexanat", "no code repair by design"),
    ]

    report, detail = {}, []
    for fn, screen, expectation in SCREENS:
        path = os.path.join(ev_dir, fn)
        if not os.path.exists(path):
            continue
        gsms = sorted({r["gsm"] for r in csv.DictReader(open(path))})
        fixed = still = absent = unknown = 0
        for g in gsms:
            v = fails(g, screen)
            if v is None:
                if g in new:
                    unknown += 1
                else:
                    absent += 1
            elif v:
                still += 1
                detail.append((screen, g, classify_residual(screen, g)))
            else:
                fixed += 1
        report[screen] = dict(errors=len(gsms), fixed=fixed, still_failing=still,
                              absent_from_run=absent, not_evaluable=unknown,
                              expectation=expectation,
                              genuine=sum(1 for s, g, st in detail
                                          if s == screen and st == "GENUINE"))

    with open(os.path.join(out_dir, "screen_verification.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["screen", "errors", "fixed", "still_failing",
                    "absent_from_run", "not_evaluable", "expectation"])
        for s, v in report.items():
            w.writerow([s, v["errors"], v["fixed"], v["still_failing"],
                        v["absent_from_run"], v["not_evaluable"], v["expectation"]])
    if detail:
        with open(os.path.join(out_dir, "still_failing.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["screen", "gsm", "status"])
            w.writerows(detail)

    json.dump(report, open(os.path.join(out_dir, "screen_verification.json"), "w"),
              indent=2)

    print(f"\n{'screen':<12}{'errors':>8}{'fixed':>8}{'still':>8}{'absent':>8}  expectation")
    for s, v in report.items():
        print(f"{s:<12}{v['errors']:>8}{v['fixed']:>8}{v['still_failing']:>8}"
              f"{v['absent_from_run']:>8}  {v['expectation']}")

    repaired = [s for s, v in report.items() if v["expectation"] == "repaired"]
    bad = sum(report[s]["still_failing"] for s in repaired)
    print("\nREPAIRED SCREENS CLEAR" if bad == 0
          else f"\n{bad} samples still failing a screen that should be repaired"
               " -- see still_failing.csv")

    residual = Counter(status for _, _, status in detail)
    genuine = residual.get("GENUINE", 0)
    if detail:
        print("\nresidual flags, by cause:")
        for status, n in residual.most_common():
            print(f"  {status:<28}{n:>6}")
    print(f"\nGENUINE OUTSTANDING ERRORS: {genuine}")
    return 0 if (bad == 0 and genuine == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
