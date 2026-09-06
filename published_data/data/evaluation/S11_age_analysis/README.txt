Table S11 / Table 1 Age row - age value composition
===================================================

age_composition.py    classifies every final_Age value into chronological / descriptor /
                      non_age / implausible / not_specified and writes age_composition.csv.
age_composition.csv   the result.

Run
---
    python3 age_composition.py \
        ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz \
        age_composition.csv

Expected
--------
chronological            361,679    44.96%
descriptor                12,912     1.61%
non_age                    3,596     0.45%
implausible                4,986     0.62%
not_specified            421,254    52.37%
total spurious             8,582     2.24% of age labels
age labels extracted     383,173    47.63%

The spurious classes (non_age + implausible) are carried into the main text's
Error analysis section; see ../error_analysis/.
corpus total             804,427

Definitions
-----------
"Age labels extracted" (Table 1, Table S3) is every class except not_specified: 383,173,
of which 370,261 carry a number and 12,912 do not. Of the numeric values, 361,679 parse to
a plausible human age, 3,596 carry a non-age unit or keyword (passage number, assay time
point, hours post-treatment, dose, barcode) and 4,986 exceed 120 years or carry no readable
number. The last two are the "spurious" rows of Table S11 and are excluded from age
summaries and from the Figure S1 histogram; descriptors are genuine extraction results the
sample metadata gave in words rather than numbers ("adult", "newborn", "young adult").
