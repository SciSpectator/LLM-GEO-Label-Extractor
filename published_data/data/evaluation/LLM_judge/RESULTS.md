# LLM-judge OOV audit — result

Independent judge = Claude (non-Gemma, §8). Validated against 150 human adjudications: Cohen's κ = 1.0.
Sample = 2,000 OOV values (stratified, oversampling high-instance values) drawn from the distinct
dictionary OOV values, the same per-value basis S7 tabulates; 1,969 are decidable audit items.
Selection probabilities were not retained, so the percentages describe the adjudicated sample and
are not design-weighted population estimates.

**Inappropriate-OOV (recall failure) rate:**
- unweighted among decided audit items: 16.29% [95% Wilson CI 14.71–18.01] (312/1,915)
- occurrence-weighted: between 23.67% and 26.32% — reported as bounds, see below
- appropriate OOV = 83.71% of audited values; 54 indeterminate excluded.

Reproduce all of it with `judge_rates.py <final corpus> .`

## Why the occurrence weight is a bound and not a number

Two properties of the released corpus make per-occurrence attribution impossible to reconstruct:

1. **Phase 2 resolves short forms per study.** A dictionary entry's top-level `target`/`id`/`source`
   is the corpus-wide default the collision guard withholds on; the per-study decisions live in its
   `by_gse` map. `HT29` is top-level OOV but resolves to CVCL_0320 across 41 studies; `Lung` is
   top-level OOV but D008168 across 78.
2. **A multi-label cell carries several ids but one `final_<field>_source`,** and its raw components
   are not positionally aligned with its ids — components normalizing to the same concept collapse
   (three raw Condition components → two ids). Of the 4,084 cells carrying an `AML` component,
   3,579 carry D015470 and 109 carry *both* D015470 and an OOV id, so that component is at once
   resolved and not.

| bound | weight | result |
|---|---|---|
| upper | every sample carrying the value (the dictionary's `count`) | 36,312/137,985 = 26.32% |
| lower | only samples whose field assignment stayed wholly OOV | 29,057/122,763 = 23.67% |

The upper bound charges each verdict with occurrences the corpus did resolve; the lower misses OOV
components sitting inside multi-label cells. The true figure lies between them. The value-level rate
(16.29%) is unaffected by any of this — it counts dictionary values, not samples.

Contrast: deterministic exact-match lower bound = 77/26/18 (S7); 78/27/19 (S14) ≈ 0.2% of OOV values.
The judge finds additional recall misses, driven by acronyms/plurals MeSH exact-match misses
(NSCLC, PBMC, CLL, AML, SLE, Placebo, CD4+ cells...).

Caveat: this MeSH build lacks some 2020s drug approvals → Treatment inappropriate-OOV is a slight
lower bound. Single judge family; scale to a 2nd judge + PPI over the full dictionary to tighten further.
Files: t5_shard*_judge.csv, judge_rates.py, merge_score.py, calibration_sheet.csv (κ), JUDGE_PROMPT.md.
