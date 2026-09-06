# LLM-judge prompt — multi-label extraction PRECISION

Judge = Claude (non-Gemma), same family/validity basis as the OOV audit (κ=1.0 calibration).

## System prompt (verbatim)
> You judge multi-label EXTRACTION precision. A sample was assigned several ';'-separated labels for one
> field (Tissue/Condition/Treatment). Given the sample's raw metadata (title, source_name, characteristics),
> decide for EACH label whether it is a correct label for THIS sample — i.e. supported by the metadata and
> the right field. Do not judge completeness (missing labels) — only the labels shown.
> For each label output: correct / incorrect / indeterminate, with a ≤10-word reason.
> A label is correct if the metadata supports that concept for this sample (synonyms count). It is incorrect
> if it contradicts the metadata, is a duplicate already listed, or is the wrong field. 'Not Specified' mixed
> with real labels = incorrect.

## Per-item user prompt (template)
> field: {field}
> extracted labels: {labels}
> raw metadata — title: {raw_title} | source: {raw_source} | characteristics: {raw_characteristics}
> Judge each label.

## Output: multilabel_judge.csv — one row per (item,label): item,field,label,verdict,reason
## Metric: precision = correct / (correct+incorrect); per-sample all-correct rate. See score_all.py (globs all shards).
