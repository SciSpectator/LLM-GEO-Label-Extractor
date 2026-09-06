# LLM-GEO-Label-Extractor — Reproducibility Package

This package contains the code and the frozen result files needed to reproduce the
results reported in the paper *"Context-aware LLM extraction and controlled-vocabulary
normalization of GEO transcriptomic sample metadata"*.

## Note on this package

This package is the local, self-contained code and data used to produce the results in
the paper. Everything needed to reproduce the reported numbers is included here; no
external repository is required.

A separate version of the software, assembled from the same work into an end-to-end
pipeline with an interactive interface, exists independently. That interface version is
not the publication artifact and is not needed to reproduce these results.

For **this publication**, Phase 1 (extraction), Phase 2 (normalization), and Age extraction were run as **separate
stages** rather than in a single end-to-end pass:
serving the fp8 `gemma-4-12B-it` model (Phase 1/1b) and the bf16 `gemma-4-E2B-it` model
(Phase 2 and Age) at the same time exceeds a single node's GPU memory, so each stage was
run independently to keep its GPU footprint within budget.

## Directory structure

Code, input files, intermediate outputs, and final results are all included so the run
can be reproduced end-to-end.

```
code/
  1_extraction_phase1_1b/   Phase 1 verbatim extraction + Phase 1b GSE-context inference,
                            Sex and Age extraction/formatting, GSE-context caching, input
                            handling (run_cli.py, phase1.py, phase1b.py,
                            sex_normalize.py, gse_context_cache.py, ...)
  2_normalization_phase2/   Phase 2 controlled-vocabulary normalization, MeSH/Cellosaurus
                            database and embedding-index construction, Phase 2 input
                            reconstruction, and the model-serving / run scripts.
                            phase2.py is the driver of record: it runs the resolution
                            cascade, then field_reroutes (removes a label filed under the
                            wrong field and, when unambiguous, moves it to the right one)
                            and casefold_oov (the final surface fold). Supporting modules:
                            phase2_mesh.py, mesh_lookup.py, cellline_db.py,
                            gse_context_cache.py, phase2_llm.py, build_mesh_db.py,
                            build_index.py, build_p1corpus.py, phase2_dedup.py,
                            serve_fleet.sh.
  4_final_assembly/         pointer to the final-corpus merge (data/merge_final_labels.py)
                            (deterministic evaluation code ships with its results under
                             data/evaluation/, one folder per table — see below)

data/
  01_input_metadata/        samples_804k.json — exact frozen GSM/GSE/GPL metadata input used by Phase 1;
                            gse_meta_scraped.json.gz — GSE study context (input to the S9 audit)
  02_phase1_1b_output/      p1_*.json.gz — Phase 1 + Phase 1b per-sample labels
                            (this is also the source that Phase 2 input is rebuilt from)
  03_age_output/            age_804k.* — the Age extraction output
  04_phase2_output/         final/<GPL>/ — Phase 2 normalized per-platform CSVs;
                            dictionary.json.gz — the resolved Phase 2 value dictionary
  05_final_labels/          LLM_labels_all_samples.csv.gz — final merged per-sample labels
                            for all 804,427 samples (the definitive result table)
  merge_final_labels.py     rebuilds 05_final_labels from Phase 2 + Phase 1/1b Sex + Age run
  evaluation/               one folder per evaluation table, each with its result CSV(s), the
                            script that produces them, and a README.txt; README.md maps every
                            supplement table (including derived/manual ones) to its source

reference/                  mesh.sqlite, cellosaurus.sqlite — controlled-vocabulary databases
                            (needed by Phase 2 and by the evaluation)
figures/                    figure_1_pipeline_schematic.png, figure_S1_label_distribution.png
```

## Pipeline flow

1. **Input.** `data/01_input_metadata/samples_804k.json` is the exact frozen input used by
   the publication run: 804,427 unique GSM records across 834 GPL platforms. Its records
   are byte-for-byte preserved from the local publication artifact; `geo_metadata.sqlite`,
   a later audit evidence store with a different GSM universe, is intentionally not included.
2. **Phase 1 + 1b.** `phase1.py` extracts verbatim Tissue/Condition/Treatment/Sex/Age
   spans; `phase1b.py` re-infers labels left "Not Specified" from the parent GSE context.
   Sex and Age are finalized here (`sex_normalize.py`).
3. **Phase 2 input.** `build_p1corpus.py` reconstructs the distinct-value corpus that
   Phase 2 consumes.
4. **Phase 2.** Build the reference databases (`build_mesh_db.py`,
   `build_cellosaurus_db.py`) and the BioLORD embedding index (`build_index.py`), serve
   the model fleet (`serve_fleet.sh`), and run normalization (`run_full.sh` / `phase2.py`).
   `phase2_casefold.py` is the final deterministic surface-fold step.
5. **Final results.** `data/merge_final_labels.py` adds Sex (Phase 1/1b) and Age (Age run)
   to the Phase 2 Tissue/Condition/Treatment output and writes
   `data/05_final_labels/LLM_labels_all_samples.csv.gz` (all five labels, 804,427 samples).
6. **Evaluation.** The deterministic metrics are reproduced from the frozen outputs alone,
   organized under `data/evaluation/` with one folder per table.

## Reproduce the deterministic evaluation

Every evaluation table has its own folder under `data/evaluation/` containing the result
CSV(s), the producing script, and a README.txt with the exact command. The main script,
`phase2_evaluation.py` (shipped in each folder it feeds — S5, S6, S7, S10, Table 3),
regenerates its seven metric CSVs bit-for-bit in one run:

```
python3 data/evaluation/S5_database_resolution/phase2_evaluation.py  data/04_phase2_output  reference/mesh.sqlite  reference/cellosaurus.sqlite  <out_dir>
```

The three single-table scripts read their inputs from `"Directory to …"` path constants set
at the top of the file:

- `data/evaluation/S14_routing_decomposition/phase2_db_match_eval.py` → S14 routing / DB-grade split
- `data/evaluation/S9_gse_shortform_audit/phase2_gse_shortform_eval.py` → S9 short-form audit
- `data/evaluation/S8_oov_consolidation/phase2_oov_consolidation.py` → S8 OOV consolidation

`data/evaluation/README.md` maps every supplement table (including the derived, snapshot,
and manual ones that have no standalone CSV) to its source.

## Frozen-data integrity

Phase 1, Age JSON/JSON.GZ, Phase 2 and the final corpus are frozen publication outputs and
must not be edited. They were checksummed before and after the package was assembled and are byte-identical;
the corrections in this release are restricted to the exact input artifact, documentation, execution wiring,
evaluation interpretation and manuscript wording. The legacy Age TSV is retained only as
a non-authoritative convenience export; the JSON representations are authoritative.

## Manual (accuracy) benchmark

The precision/recall/F1 accuracy figures reported in Table 1 and Supplementary Table S1 come
from a human evaluation: for each of the five labels the authors manually assigned the
TP/FP/TN/FN verdict on 1,500 samples. This is the only tier of the evaluation that is not a
deterministic function of the corpus.

Those per-sample verdicts ship in `data/evaluation/S1_manual_benchmark/`, one file per label,
each giving the GSM, the gold value, the pipeline's prediction and the assigned verdict, together
together with the gold each was judged against. The Tissue, Condition and Treatment rows are the
authors' own per-sample verdicts and are not produced by any script in the package. Sex and Age are
judged against the manually curated gold-standard labels from the ALE study (Giles et al. 2017, *ALE:
automated label extraction from GEO metadata*, BMC Bioinformatics 18:509) over every applicable
curated record present in the final corpus, so a sample the pipeline left unlabelled counts as a
false negative; Tissue and Condition use CREEDS concepts and Treatment uses GSM/GSE metadata. That folder also ships the two gold sources themselves, the ALE curated labels and the CREEDS
disease signatures. It ships no scoring script: the verdicts are the result, and there is
nothing for code to recompute. See its README for the sampling protocol and for what each
published row rests on.

## Models and reference data

- Phase 1 / 1b: `google/gemma-4-12B-it` (Gemma 4, arXiv:2607.02770; fp8 weight quantization, 16-bit KV cache)
- Phase 2 / Age: `google/gemma-4-E2B-it` (Gemma 4 E2B — a nested "effective-2B", ~5.1B-parameter
  natively multimodal model; arXiv:2607.02770), run in bf16 as a **text-only derivative**: the
  vision, audio, and multimodal-projector tensors are stripped and only the 601 text-decoder
  tensors are kept (text outputs bit-identical; ~14% less VRAM, ~6% higher throughput)
- Embeddings: `FremyCompany/BioLORD-2023`, used both for candidate retrieval (top-8) and for
  shortlisting out-of-vocabulary concepts before the LLM consolidation decision (0.90 cosine floor)
- Controlled vocabularies: MeSH 2026, Cellosaurus

Decoding was greedy (temperature 0); see `requirements.txt` for pinned dependencies.

### Contents of `reference/mesh.sqlite`

The vocabulary proper is held in `mesh_terms` (31,110 descriptors), `mesh_synonyms` (235,902),
`mesh_tree` (65,360) and `mesh_parent` (42,519); `reference/cellosaurus.sqlite` holds `cell_lines`.

The file also contains a few small scratch tables left over from earlier development of the
resolution engine — `resolutions` (182 rows), `verifier_decisions` (31), `polarity_decisions` (15),
`alien_clusters` (52), `alien_synonyms` (209), `oov_mesh_clusters` (2), `oov_mesh_synonyms` (2).
**They are not part of the reported results and were not consulted by the run that produced them.**
The published Phase 2 output is `data/04_phase2_output/dictionary.json.gz` (21,254 Tissue / 21,808
Condition / 48,216 Treatment distinct values), and it disagrees with those scratch rows wherever the
two overlap — for example `PBMC` resolves to a minted out-of-vocabulary concept in the published
dictionary, not to the MeSH descriptor recorded in the old `resolutions` row.

The deterministic evaluation does not read any of them either: `phase2_evaluation.py` queries only
`mesh_terms`, `mesh_synonyms`, `mesh_tree` and `cell_lines`. Every reported metric is therefore
recomputed from the controlled vocabulary and the frozen per-sample outputs, never replayed from a
stored decision.
