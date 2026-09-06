# Output schema

Each final sample retains its GSM/GSE identifiers and input metadata provenance,
plus:

- `Sex`, `Age`, `Tissue`, `Condition`, `Treatment`, the final display values
- Phase-1 and Phase-1b values for auditability
- Phase-2 canonical name and identifier for Tissue/Condition/Treatment
- source/tier (`mesh`, `cellosaurus`, or local `oov`)
- decision stage, confidence/similarity where applicable, and cache provenance

The normalization dictionary stores one decision per unique raw value and its
corpus frequency. Completion markers record finished stages but are not research
results. Audit logs and dictionaries should be retained with every release.

## Final corpus

The `assemble` stage merges the per-platform Phase 2 tables into the shipped
corpus under `<out-dir>/final_labels/`:

- `LLM_labels_all_samples.csv.gz`, every sample with all five `final_*` labels,
  in a single table
- `by_GPL/<GPL>.csv.gz`, the same rows split per platform for platform-scoped
  analyses
- `manifest.json`, sample count, platform count, Sex/Age presence counts, and the
  number of cells whose duplicate identifiers were relocated

Assembly is deterministic: it adds no new labels and calls no model. Where a
normalized cell repeats an identifier (a raw value that fragmented across
studies), the duplicate label and its identifier are dropped so one concept
cannot appear twice in the same cell.
