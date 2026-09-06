Table S15 / Table S17 - Condition control vs disease split
==========================================================

condition_control_split.py   recomputes the explicit healthy/control and disease/non-control
                             counts from the final corpus and the control label set defined
                             in the pipeline source.
condition_control_split.csv  the result.

Run
---
    python3 condition_control_split.py ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz ../../../code/2_normalization_phase2/phase2_mesh.py condition_control_split.csv

Expected
--------
condition labels (basis)     646,948    80.42%
healthy/control               83,914    10.43%
disease/non-control          563,034    69.99%
corpus total                 804,427

Definitions
-----------
Basis: samples carrying a Condition label after Phase 1b, excluding the "unknown" tokens
that Table S3 counts as Not Specified. Control: every label in the sample's final Condition
cell is one of the 19 canonical control surfaces of _CONDITION_CONTROL_CANONICAL
(code/2_normalization_phase2/phase2_mesh.py). A sample carrying both a control label and a
disease label counts as disease. The two categories partition the basis exactly:
83,914 + 563,034 = 646,948.

The corpus above is the repaired one, which is what this table reports.
Passing ../../05_final_labels/LLM_labels_all_samples.csv.gz instead reruns the
first pass and gives different counts that appear nowhere in the paper.
