# Repaired pipeline

The released pipeline with a bounded set of repairs. Function and module names
are unchanged, so a reader can diff this tree against the released one directly:

```bash
diff -rq code/1_extraction_phase1_1b code/5_repaired_pipeline/phase1
diff -rq code/2_normalization_phase2 code/5_repaired_pipeline/phase2
```

**Phase 1 is byte-identical** — all 15 files. Extraction was not modified.
**Phase 2** has 14 files unchanged, 6 repaired and 3 added. No function was
renamed or removed; the repairs add functions and change the body of existing
ones.

## What was repaired, by function

| File | Function | Repair |
|---|---|---|
| `cellline_db.py` | `_exact_unambiguous` (new), `_accession`, `match_ref` | a catalogue name matching more than one entry no longer resolves to whichever row was read first |
| `cellline_db.py` | `donor_sex` (new) | exposes the catalogued donor sex so a contradiction with the extracted Sex is detectable |
| `build_cellosaurus_db.py` | `main` | carries `organism` and `donor_sex` into the database; without them no downstream check is possible |
| `build_index.py` | `FormTable.add`, `FormTable.unambiguous` (new) | surface forms mapping to several identifiers are withheld from the retrieval index and written to a collisions report |
| `phase2_mesh.py` | `_expand_shortform` | consults the study's own definition before the model |
| `phase2_mesh.py` | `_expansion_spells` (new) | accepts a model expansion only if the concept it resolves to spells the abbreviation, tested over every name that concept carries |
| `phase2_mesh.py` | `_resolve_one_global` | tier 0 refuses values that are only stage or grade scaffolding |
| `phase2.py` | `vocabulary_hit`, `reconcile_vocabulary` (new) | an out-of-vocabulary identifier is no longer minted for a term the vocabulary holds |
| `phase2.py` | `samples_of` | passes the samples carrying a value into the short-form stage |
| `mesh_lookup.py` | `forms_of` (new) | returns every name a concept carries, not just its heading |
| `assembly/merge_final_labels.py` | `reconcile_sex_with_cellline`, `reconcile_sex_with_anatomy` (new) | an attested donor sex, and sex-specific anatomy, outrank a Sex inferred from prose |

Added modules:

| File | Purpose |
|---|---|
| `acronym_expand.py` | deterministic definition extraction by initial-letter alignment, plus the short-form prompt contract |
| `qualifier_guard.py` | recognises a value made only of stage or grade scaffolding |
| `build_vocab_from_meshdb.py` | builds the `vocab` table from `mesh.sqlite`, since the released build tool reads a MeSH XML release the package does not ship |

`PROVENANCE.md` gives the size of each change and the defect it closes.

## Evaluation

`evaluation/` holds the scorers used on the re-extracted samples:
`evaluate_acronyms.py` scores acronym expansion per sample, and
`verify_screens.py` re-runs the six integrity screens and classifies whatever
still trips one.

The scripts that build the corpus from the re-extracted samples are under
`data/06_final_labels_repaired/scripts/`.
