# Table 4 — integrity screens on the first-pass labels

`table4_screens.py` recomputes the five main-text Table 4 screens from the
released data and prints what it gets beside what the table prints. Nothing is
copied from the manuscript except the published values used for that comparison.

## Run it

```bash
S=../S23_integrity_screens
PYTHONPATH=$S AGE_MODULE=$S/age_composition.py python3 table4_screens.py \
    ../../05_final_labels/LLM_labels_all_samples.csv.gz \
    ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz \
    ../../../reference/mesh.sqlite \
    ../../../reference/cellosaurus_repaired.sqlite \
    table4_screens.csv
```

The four Sex and cell-line screens are measured on the **first-pass** label file
in `data/05_final_labels/`, because Table 4 reports them as found, before the
corrective re-extraction. The late-onset row is measured on the repaired corpus,
which is where it reproduces, in both its count and its base. Both files are
therefore passed in and the script says which row uses which. Cell-line
attributes come from `cellosaurus_repaired.sqlite`, the only build carrying
`organism` and `donor_sex`.

## Where the definitions come from

The screen predicates are those in
`../S23_integrity_screens/verify_screens.py`, so Table 4 and Table S23 measure
the same thing on two different corpora. Sex-specific anatomy and sex-specific
condition are read off the MeSH tree, not from a hand-written list:

| screen | definition |
|---|---|
| sex vs anatomy | `A05.360.319` female, `A05.360.444` male, both minus `A16` |
| sex vs condition | `C12.050.351.500`, `C12.100.250`, `C12.050.703` female, `C12.100.500`, `C12.200.294` male, both minus `C16` and `C12.050.703.824` |
| cell-line donor sex | `donor_sex` from the catalogue disagrees with the extracted Sex |
| cell-line species | a catalogued line whose `organism` is not Homo sapiens |
| late-onset vs age | `D000544`, `D010300` or `D029424` **with their MeSH tree descendants**, on a sample whose age parses below 18 years |

Counting follows the caption. `n comparable` is assignments, not samples, so a
sample carrying two cell lines contributes two.

The sex-vs-condition row counts only the part that is **not** staging-driven. A
Condition made of pure stage or grade scaffolding that still received a MeSH
identifier is a staging failure, repaired separately and reported in Table S23.
The script separates them with the shipped `qualifier_guard`, and the split it
finds is 184 staging-driven of 239 flags, leaving 55.

## What reproduces, and what does not

Eight of the ten cells reproduce exactly:

| cell | script | table |
|---|---|---|
| sex vs anatomy, contradictions | 11 | 11 |
| sex vs condition, contradictions | 55 | 55 |
| cell-line donor sex, n | 79,551 | 79,551 |
| cell-line donor sex, contradictions | 571 | 571 |
| cell-line species, n | 167,476 | 167,476 |
| cell-line species, contradictions | 1,076 | 1,076 |
| late-onset, n | 4,102 | 4,102 |
| late-onset, contradictions | 348 | 348 |

Two do not, and the script reports them rather than hiding them:

| cell | script | table |
|---|---|---|
| sex vs anatomy, n | 9,613 | 9,522 |
| sex vs condition, n | 3,975 | 3,761 |

Both are denominators, and in both the script counts a slightly wider base than
the table: 91 more comparable assignments for anatomy and 214 more for
condition. The findings those two rows report, the contradiction counts, are
unaffected and reproduce exactly. No definition was adjusted in order to close
these two gaps, because a screen tuned until its output matches a target is not
a reproduction of anything.
