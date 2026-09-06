Table S3. Phase 1b recovery of "Not Specified" labels using GSE context.

Result file in this folder: phase1b_recovery.csv
Generating script in this folder: phase1b_recovery.py

Run (from this folder):
  python3 phase1b_recovery.py ../../02_phase1_1b_output \
      ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz \
      phase1b_recovery.csv

The two halves of this table come from different files, and deliberately so.
The corpus does not keep a Phase 1 column: what a sample looked like before
GSE-context inference survives only in ../../02_phase1_1b_output, while the
label it ended up with is in the released corpus. Taking each column from the
file that holds it keeps the Phase 1b coverage identical to what Table S15
reports for the same fields, instead of to an earlier extraction pass.

Expected:
  Tissue     NS@P1   8,830  recovered   6,762 (76.58%)  98.90% -> 99.74%
  Condition  NS@P1 289,514  recovered 132,094 (45.63%)  64.01% -> 80.43%
  Treatment  NS@P1 488,270  recovered  53,154 (10.89%)  39.30% -> 45.91%
