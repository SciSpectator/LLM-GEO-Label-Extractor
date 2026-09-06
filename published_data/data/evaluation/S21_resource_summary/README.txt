Table S21 - what the released corpus contains
=============================================

resource_summary.py    counts the distinct concepts the corpus assigns, by field and
                       vocabulary, and the samples each group covers.
resource_summary.csv   the result.

Run
---
    python3 resource_summary.py \
        ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz resource_summary.csv

Expected
--------
field      vocabulary      concepts    samples  % corpus
Tissue     MeSH               672    482,725     60.01
Tissue     Cellosaurus      2,599    166,041     20.64
Tissue     OOV              6,126    137,598     17.11
Condition  MeSH             1,267    407,126     50.61
Condition  OOV              9,759    249,552     31.02
Treatment  MeSH             1,502     85,435     10.62
Treatment  OOV             35,921    263,661     32.78

distinct concepts in the corpus             57,846
distinct MeSH descriptors (union of fields)  3,441

Distinct concepts versus distinct raw values
--------------------------------------------
Table S2 counts distinct raw values, the surface forms that enter normalization. This table
counts distinct concepts, the identifiers that leave it. The two differ by the collapse the
pipeline performs: Tissue routes 6,501 distinct raw values to Cellosaurus, and those resolve
to 2,708 distinct cell lines, because many surface forms name the same line (MCF7, MCF-7,
MCF 7). Reading either number as the other overstates or understates the vocabulary the
corpus actually spans.

A sample is counted once per group even when it carries several labels of that group, so the
sample columns do not sum to the corpus size.
