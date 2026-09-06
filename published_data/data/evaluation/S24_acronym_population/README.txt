Table S24. The acronym-shaped Condition population and what it resolved to.

Table S6 scores a curated acronym list against a gold concept. This table takes
the whole population instead: every acronym-shaped Condition label the corpus
contains, so a reader sees what the pipeline actually faced rather than a chosen
subset, and how much of it received a controlled-vocabulary concept.

Result file in this folder:
  acronym_population.csv   one row per acronym listed at >=100 occurrences:
                           acronym, occurrences, concept, resolved,
                           instances_resolved

Generating script in this folder: acronym_population.py

Run (from this folder):
  python3 acronym_population.py \
    ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz \
    ../../../code/5_repaired_pipeline/phase2 \
    acronym_population.csv

Two definitions matter and neither is invented here.

"Acronym-shaped" is `_is_acronym_shaped` imported from
`code/5_repaired_pipeline/phase2/acronym_expand.py` — the same predicate the
pipeline uses to decide what to route through study-scoped expansion, so the
population is the one the pipeline itself recognises rather than a shape rule
chosen after the fact.

The measurement is taken on samples whose Condition cell holds a single label.
A multi-label cell carries several ids under one `final_Condition_source`, and
its raw components are not positionally aligned with those ids because
components normalizing to the same concept collapse; an acronym inside such a
cell cannot be scored without apportioning. Restricting to single-label cells
makes each sample's outcome exact. The excluded multi-label cells are reported
in Table S8 and the multi-label supplementary note.

What the script prints, and what the caption reports:
  827 distinct acronym-shaped Condition labels
  65,696 occurrences, of which 51,654 (78.6%) resolve to a controlled concept
  87 acronyms listed at >=100 occurrences, covering 54,640 occurrences

Inputs shipped for this table:
  ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz
  ../../../code/5_repaired_pipeline/phase2/acronym_expand.py
