Table S27 - benchmark performance re-aggregated by study
========================================================

The reviewer observed that scoring every sample as an independent observation lets a
few large, well-annotated experiments carry the result. This folder answers that by
re-aggregating the SAME hand-assigned verdicts of Table S1 with every GSE weighted
equally, whatever its size.

Nothing here re-judges a sample. study_level_metrics.py reads the verdict column of
S1_manual_benchmark and never writes it.

  map_gse.py                 maps each benchmark GSM to its parent GSE by streaming
                             data/01_input_metadata/samples_804k.json one record at a
                             time (the file is 429 MB). Writes gsm_gse.csv.
  gsm_gse.csv                the mapping; 5,598 accessions, all resolved.
  study_level_metrics.py            recomputes Table S1 under the corrected category split and
                             Tables S27 and S28 by study. Writes the CSV below.
  study_level_metrics.csv   one row per label with both aggregations.
  verify_sexage.py           confirms the corrected Sex false-negative count against ALE
                             directly: 3,493 curated records returned as Not Specified.
  age_unparse.py             splits the corrected Age false negatives into 20 with no
                             output and 39 whose output carries no number.

Run
---
    python3 map_gse.py
    python3 study_level_metrics.py

Expected
--------
Tissue    222 GSE, median 4 samples/study, per-sample 0.9967, per-study 0.9927
Condition 222 GSE, median 4 samples/study, per-sample 0.9500, per-study 0.9422
Treatment 971 GSE, median 1 sample/study,  per-sample 0.8660, per-study 0.8300
Sex       171 GSE, per-sample 0.9987, per-study 0.9997
Age       125 GSE, per-sample 0.9860, per-study 0.9915

The Sex and Age rows are the 1,500-sample hand-checked subsets, NOT the full ALE
overlap that Table S1 reports. Do not read them against that table's recall.

The largest effect anywhere is Treatment recall: 0.847 per sample, 0.790 per study.
The benchmark is not dominated by consortium studies - the ten largest series hold
under a fifth of it in every field.
