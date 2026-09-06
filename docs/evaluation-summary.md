# Evaluation summary (short report)

This is a one-page summary of the manuscript evaluation, for readers who want the
headline numbers without the full paper. It reproduces only aggregate results.
The evaluation code, the gold standards and the frozen per-sample outputs behind
these numbers are in [published_data/](../published_data/), one folder per table.

## Phase 1, extraction accuracy

Measured on the 804,427-sample corpus against manual gold labels. Sex and Age
use every applicable ALE-curated record (n = 9,299 and 5,164). Tissue and
Condition use 1,500 CREEDS samples, and Treatment uses 1,500 GSM/GSE-adjudicated
samples. A missing output counts as a false negative and a mismatch as both a
false positive and a false negative, so recall measures completeness over the
curated records.

These are two separate tables, so that no row joins a corpus count to a benchmark
score. Left: how many of the 804,427 samples carry a label after Phase 1b.
Right: how accurate those labels are, each row measured only on the benchmark
whose size is given in the same row.

<table>
<tr><td valign="top">

| Field | Labels extracted | % of 804,427 |
|---|---:|---:|
| Sex | 319,745 | 39.75% |
| Age | 383,173 | 47.63% |
| Tissue | 802,359 | 99.74% |
| Condition | 647,007 | 80.43% |
| Treatment | 369,311 | 45.91% |

</td><td width="40"></td><td valign="top">

| Field | Benchmark n | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| Sex | 9,299 | 0.997 | 0.623 | 0.767 |
| Age | 5,164 | 0.987 | 0.976 | 0.982 |
| Tissue | 1,500 | 0.997 | 0.997 | 0.997 |
| Condition | 1,500 | 0.964 | 0.950 | 0.957 |
| Treatment | 1,500 | 0.976 | 0.847 | 0.907 |

</td></tr>
</table>

Precision is high across every field. The lower Sex recall is completeness, not
error: it counts curated records the pipeline returned as "Not Specified" rather
than answering wrongly. Coverage, the share of samples labelled before Phase 2,
is the "% of samples" column.

## Phase 2, database resolution

Controlled-vocabulary matching on the exact-name/synonym in-dictionary slice
(instance-weighted). A wrong database identifier counts as both a false positive
and a false negative, and an answerable label left out-of-vocabulary counts as a
false negative. Accuracy is TP over gold-positive answerable items.

| Field | Labels with DB answer | Precision | Recall | F1 | Accuracy | Wrong DB ID | No DB ID (OOV) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Tissue | 342,821 | 0.988 | 0.998 | 0.993 | 0.985 | 4,246 | 791 |
| Condition | 219,651 | 0.994 | 0.998 | 0.996 | 0.993 | 1,225 | 382 |
| Treatment | 23,261 | 0.988 | 0.994 | 0.991 | 0.982 | 271 | 149 |

## Whole-corpus entity linking

Across all 804,427 samples, Phase 2 collapsed the raw label vocabulary onto a
much smaller set of canonical concepts:

| Field | Unique label values | Canonical | to MeSH | to Cellosaurus | to OOV | Uncovered | MeSH-branch violations |
|---|---:|---:|---:|---:|---:|---:|---:|
| Tissue | 21,254 | 10,548 | 8,068 | 6,340 | 6,682 | 164 | 0.00% |
| Condition | 21,808 | 15,679 | 11,332 | n/a | 9,148 | 1,328 | 0.44% |
| Treatment | 48,216 | 40,287 | 6,988 | n/a | 39,201 | 2,027 | 2.07% |

In total 26,388 distinct labels resolved to MeSH descriptors and 6,340 Tissue
labels to Cellosaurus identifiers (e.g. `MCF7 -> CVCL_0031`). Out-of-vocabulary
labels are clustered into local concepts that preserve specificity rather than
being forced onto an incorrect MeSH hypernym.

## Reported limitations (abbreviated)

- Performance estimates depend on the benchmark: a disease-focused CREEDS subset
  does not establish general performance across all GEO study types.
- **Treatment** is the hardest field: the evidence may reside in protocol text,
  group labels, or series design, and no curated resource provides comprehensive
  treatment gold labels.
- **MeSH is limited** in the concepts it covers.
- Semantic normalization is deliberately conservative: control, healthy and
  normal are conceptually similar but are not merged, and marker variants such as
  CD4 and CD4+ are kept apart.
- The Phase 2 F1 measures controlled-vocabulary matching only for labels that
  have a MeSH or Cellosaurus answer, not out-of-vocabulary clustering. Beyond
  that bound an independent LLM judge from a different model family was used.

Numbers are drawn from Tables 1 to 3 of the main text.
