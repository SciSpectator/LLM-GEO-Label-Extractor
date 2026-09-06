Table S20 - provenance of the extracted Age values
==================================================

age_provenance.py    classifies every returned Age value by what the sample's own record
                     offers as its source.
age_provenance.csv   the result.

Run
---
    python3 age_provenance.py ../../01_input_metadata/geo_metadata.sqlite ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz age_provenance.csv

Expected
--------
class                     values       %   day/week   % d/w
donor age field          299,546   78.01      7,648    2.55
developmental field        2,408    0.63        502   20.85
no age field              82,040   21.36     27,311   33.29
total                    383,994  100.00

What this separates
-------------------
The Age prompt requires the value to come from a characteristics field whose name is or
contains "age" - including gestational age, postnatal day and developmental stage - and
only then allows a fallback to the title, source or description for that subject's own age.
Years, months, weeks and days are all admissible units, so a sub-year value is not
anomalous in itself; a postnatal "day N" is specified to yield "days: N".

    donor age field       a characteristics key names an age ("age", "age at diagnosis",
                          "donor age", "patient age (yrs)"). Specification-compliant, and
                          only 2.55% of these are day- or week-denominated.
    developmental field   no age key, but a key names a gestational, postnatal, embryonic
                          or developmental quantity. Also intended; 20.85% day/week, as
                          expected for developmental material.
    no age field          the record names neither, so the value came from free text. This
                          is where the specification is not met, and a third of these
                          (33.29%) are day- or week-denominated experimental timepoints
                          such as a "Control Day0" title read as the subject's age.

Keys are matched on "age" as a whole token, which excludes keys that merely contain the
letters (stage, agent, storage, passage, dosage, lineage, percentage, average, massage).

Consequence for reuse
---------------------
Age-stratified analyses should restrict to the 299,546 values backed by a named age field,
optionally adding the 2,408 developmental ones. The remaining 82,040 are not separable into
genuine free-text ages and misread timepoints without a manual gold standard, which has not
been built.

Before running anything that reads geo_metadata.sqlite, build it once from the
shipped JSON -- the database is an index over samples_804k.json and is not
shipped twice:
    cd ../../01_input_metadata && python3 build_geo_metadata.py \
        samples_804k.json geo_metadata.sqlite

The corpus above is the repaired one, which is what this table reports.
Passing ../../05_final_labels/LLM_labels_all_samples.csv.gz instead reruns the
first pass and gives different counts that appear nowhere in the paper.
