                      

"""GSM -> GSE for the benchmark samples only.

The input file is a 429 MB JSON array and the login node has 8 GB free, so it is
decoded one object at a time rather than loaded whole. Only the accessions the
benchmark actually uses are kept; the rest are discarded as they stream past.
"""



import csv

import json

import os



import os as _os
PKG = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", ".."))

GOLD = f"{PKG}/data/evaluation/S1_manual_benchmark"

SRC = f"{PKG}/data/01_input_metadata/samples_804k.json"

OUT = _os.path.join(_os.path.dirname(__file__), "gsm_gse.csv")



wanted = set()

for f in ("manual_gold_Tissue_Condition.csv", "manual_gold_Treatment.csv",

          "manual_gold_Sex.csv", "manual_gold_Age.csv"):

    with open(os.path.join(GOLD, f)) as fh:

        wanted |= {r["gsm"] for r in csv.DictReader(fh)}

print(f"  benchmark accessions: {len(wanted):,}")



dec = json.JSONDecoder()

found = {}

with open(SRC) as fh:

    buf = fh.read(1 << 20)

    i = buf.index("[") + 1

    while True:

        while i < len(buf) and buf[i] in " \t\r\n,":

            i += 1

        if i >= len(buf) or buf[i] == "]":

            chunk = fh.read(1 << 20)

            if not chunk:

                break

            buf = buf[i:] + chunk

            i = 0

            continue

        try:

            obj, end = dec.raw_decode(buf, i)

        except ValueError:                                                       

            chunk = fh.read(1 << 20)

            if not chunk:

                break

            buf = buf[i:] + chunk

            i = 0

            continue

        if obj.get("gsm") in wanted:

            found[obj["gsm"]] = obj.get("gse", "")

        i = end



print(f"  resolved: {len(found):,}   missing: {len(wanted - set(found)):,}")

with open(OUT, "w", newline="") as fh:

    w = csv.writer(fh)

    w.writerow(["gsm", "gse"])

    for gsm in sorted(found):

        w.writerow([gsm, found[gsm]])

print(f"  wrote {OUT}")

