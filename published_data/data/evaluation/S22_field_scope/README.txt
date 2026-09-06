Table S22 - cross-field scope: what was removed, what was moved, what it cost
=============================================================================

field_scope_audit.py    counts the labels the field-scope step removed or moved, and checks
                        whether the field that should hold the concept already carried a label.
field_scope_audit.csv   the result.

Run
---
    python3 field_scope_audit.py ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz field_scope_audit.csv

Expected
--------
case                    action     samples  target labelled       %  target CVCL
disease in Tissue       removed     20,396            20396  100.00
cell line in Condition  removed      1,057             1057  100.00          405
cell line in Treatment  removed      2,063             2063  100.00          658
moved to Condition      moved          156
moved to Tissue         moved           28

removed in total           23,516, of which the target field already had a label 23,516 (100.00%)
moved in total                184

What the step does
------------------
A field must hold only its own kind of concept. After resolution, phase2.py::field_reroutes
applies a factual controlled-vocabulary test, not a fuzzy merge:

    disease in Tissue        the value resolves to exactly one MeSH disease (C or F branch)
                             and to no anatomy (A branch)
    cell line in Condition   the value is a catalogued Cellosaurus line
    or in Treatment

The mismatched value is removed from the field it does not belong to, and where the target is
unambiguous the concept is moved there instead.

Why the removals are not a loss of information
----------------------------------------------
For every one of the 23,516 removals the field that should hold that kind of concept already
carried a label: a sample whose Tissue held a disease already had a Condition, and a sample
whose Condition or Treatment held a cell line already had a Tissue. No sample was left with an
empty target field. Of the 3,120 cell-line removals, 1,063 have a Cellosaurus identifier in
Tissue, so the line itself is retained there; for the remaining 2,057 the Tissue field names
the material in another way (for example a cell type rather than the line).

The 184 moves are the subset where the target concept was unambiguous, so the value was
re-filed rather than dropped.

The corpus above is the repaired one, which is what this table reports.
Passing ../../05_final_labels/LLM_labels_all_samples.csv.gz instead reruns the
first pass and gives different counts that appear nowhere in the paper.
