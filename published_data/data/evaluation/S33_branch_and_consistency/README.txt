Table S33 - MeSH branch validity and identical-input consistency
================================================================

Both screens run over the whole corpus and neither needs a gold standard. They were
quoted in the Results before this revision without a table of their own, which is why
they now have one.

  branch_validity.csv   MeSH descriptors assigned outside the tree branch their field is
                        allowed to draw from.
  consistency.csv       raw values occurring more than once, and how often their
                        occurrences did not all receive the same target.
  phase2_evaluation.py  the script that writes both, copied here from
                        Table3_normalization_quality where it also produces Table 3.

Run
---
    python3 phase2_evaluation.py \
        ../../06_final_labels_repaired/phase2_out \
        ../../../reference/mesh.sqlite ../../../reference/cellosaurus.sqlite .

Expected
--------
branch_validity.csv
    field       checkable   violations   rate
    Tissue          8,434            0   0.00%
    Condition      11,332           51   0.44%
    Treatment       6,988          337   2.07%

consistency.csv
    field       groups   disagreeing   rate
    Tissue       1,746           329   18.84%
    Condition    1,766           388   21.97%
    Treatment    1,557           264   16.96%

Pass the repaired Phase 2 output as shown. Earlier drafts of the paper carried
0.52%/2.23% and 18.44%/20.16%/15.74%; those came from a superseded run, do not
reproduce from any shipped artefact, and were corrected in this revision.

Reading the disagreement rate
-----------------------------
A disagreement is not by itself an error. Phase 2 expands ambiguous short forms against
the study that wrote them, so one abbreviation can legitimately resolve to different
concepts in different studies; Table S9 audits those expansions directly. This screen
measures how often that happens at all, which is the honest thing to report alongside
the claim that a value is decided once.
