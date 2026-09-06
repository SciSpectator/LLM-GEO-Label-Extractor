Table S13. Baseline comparison on the same data. coverage_of_extracted_labels, accuracy_answerable_slice and f1_answerable_slice are deterministic and reproducible from the shipped Phase 2 outputs + reference DBs. accuracy_manual_gold (rule-based extraction) is scored against the SAME author-held manual gold as Table 1/S1 (ALE curation for Sex/Age; CREEDS/metadata for T/C/T); that gold now ships in ../S1_manual_benchmark/ together with its scorers.

Reproduce the two coverage rows:
  python3 s19_baseline_coverage.py ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz ../../../reference/mesh.sqlite out.csv

Metric rows in baseline_comparison.csv:
  accuracy_manual_gold        extraction accuracy against the 1,500-per-label manual gold.
  coverage_of_extracted_labels
                              fraction of the labels the pipeline extracted (samples with a non-"Not
                              Specified" Phase 1b value: Tissue 802,359, Condition 647,007, Treatment
                              369,311) that carry a MeSH or Cellosaurus identifier. Table S10 reports
                              the same assignments against all 804,427 corpus samples, so the two
                              denominators differ and the two tables are not comparable directly.
  accuracy_answerable_slice   accuracy on the database-answerable slice; the "ours" column equals the
                              instance-weighted accuracy in ../S5_database_resolution/db_resolution.csv
                              (Tissue 0.9853, Condition 0.9927, Treatment 0.9819).
  f1_answerable_slice         F1 on the database-answerable slice; the "ours" column equals the
                              instance-weighted F1 in ../S5_database_resolution/db_resolution.csv
                              (Tissue 0.9926, Condition 0.9963, Treatment 0.9909).
The exact_mesh column is 1.000 on the answerable slice by construction (an exact name/synonym match is
correct by definition); it is reported for coverage, not accuracy, in the printed table.

Result file in this folder: baseline_comparison.csv
Generating script: s19_baseline_coverage.py reproduces the two coverage rows from the shipped final
corpus and mesh.sqlite. The accuracy_manual_gold rows have no shipped scorer, because the manual gold
they are scored against ships in ../S1_manual_benchmark/ (consistent with Table 1/S1).

The Condition denominator is 647,007, the same count Table 1, Table S3, Table S15 and
Table S17 use. An earlier version of the script tested only for the exact string
"Not Specified" and so counted 70 samples whose Condition reads "unknown" as
labelled, giving 647,077 and a Condition coverage of 62.90% instead of 62.91%.
