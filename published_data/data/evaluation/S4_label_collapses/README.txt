Table S4 - representative raw-label collapses
=============================================

The table lists raw strings that Phase 2 routed onto one canonical concept, with
the number of samples carrying each string. Those counts are measurements over
the released corpus, not illustrations, so they are recomputed here rather than
left standing on the table alone.

  label_collapses.csv   the result: one row per raw string, with its sample
                        count, the identifier those samples ended up with, and
                        the share of them that carry it.
  s4_collapse.py        the script that produces it.

Run:
  python3 s4_collapse.py \
      ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz \
      label_collapses.csv

A count is the number of samples whose phase1b value for that field is exactly
the listed string. The concept is what those samples carry after Phase 2.

The share column is worth reading. Three strings do not collapse uniformly:
'whole blood' resolves to the tabulated concept in 99.75% of its samples,
'Whole blood' in 98.62% and 'Vehicle' in 93.01%. The remainder resolve
elsewhere because Phase 2 decides per study wherever the study's own context is
decisive, which is the behaviour Table S34 describes. The table prints the
dominant collapse; this file prints how dominant it is.
