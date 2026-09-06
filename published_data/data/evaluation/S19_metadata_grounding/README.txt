Table S19 - grounding of Sex and Age in the sample's own metadata
=================================================================

grounding_audit.py    classifies every Sex and Age decision over the whole corpus by
                      whether the sample's own record supports it.
grounding_audit.csv   the result.

Run
---
    python3 grounding_audit.py ../../01_input_metadata/geo_metadata.sqlite \
        ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz grounding_audit.csv

Expected
--------
Sex: values 319,745 (grounded 319,657 = 99.97%, ungrounded 88 = 0.03%)
     Not Specified 484,682 (justified 478,050 = 98.63%, token present 6,632 = 1.37%)
Age: values 383,173 (grounded 357,524 = 93.31%, ungrounded 25,649 = 6.69%)
     Not Specified 421,254 (justified 299,165 = 71.02%, token present 122,089 = 28.98%)

The Sex 'token present' count and both ungrounded counts are carried into the main
text's error analysis; see ../error_analysis/. The Age 'token present' figure is not,
for the reason given below.

What this measures
------------------
Sex and Age are verbatim fields, so a returned value must be supported by text in the
sample's own record and "Not Specified" is the correct answer when the record does not
state the attribute. The audit therefore asks, for every one of the 804,427 samples,
whether the record contains a matching token:

    grounded value    a value was returned and the record supports it
    ungrounded value  a value was returned and the record contains no such token
    justified NS      "Not Specified" was returned and the record contains no token
    missed            "Not Specified" was returned although the record contains a token

It needs no gold standard and runs over the whole corpus, so it complements the
1,500-per-label human benchmark, which can only speak for samples a curator labelled.

Reading the Age "Not Specified" split
-------------------------------------
The script computes and stores it, but it is not quoted in Table S19 and should not be read as
a miss rate. The Sex pattern is the one the pipeline itself applies (sex_ground.py) and is
precise. No equally precise pattern exists for Age: the permissive pattern used here matches
unrelated text ("adult tissue", "3 days post-treatment"), so the 28.92% is an upper bound on
missed ages, not a measurement. The Age row of Table S19 therefore quotes only the value side,
where the direction is unambiguous - a returned value with no age-like token anywhere in the
record cannot be grounded. That set mixes two things. Some are genuine errors, an identifier or
an assay time read as an age ("donor 184", "time=0hr", "SAMPLE 30"). Others are developmental
or culture times that are legitimately the sample's own age ("days: 7", "week 0") but are not
donor chronological ages. Table S20 separates those cases by the field the record offers.

Before running anything that reads geo_metadata.sqlite, build it once from the
shipped JSON -- the database is an index over samples_804k.json and is not
shipped twice:
    cd ../../01_input_metadata && python3 build_geo_metadata.py \
        samples_804k.json geo_metadata.sqlite
