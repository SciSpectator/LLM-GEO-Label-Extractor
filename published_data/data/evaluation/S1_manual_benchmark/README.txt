Table S1 - manual accuracy benchmark
====================================

This is the human accuracy tier of the evaluation, and the only tier that is not a
deterministic function of the corpus: every other evaluation in this package (S5-S14,
Table 3) is computed automatically over the whole 804,427-sample corpus.

Two different things are shipped here, and Table S1 does not build its rows the same way
for both.

  Tissue, Condition, Treatment   the authors assigned the TP / FP / TN / FN verdict by hand,
                                 sample by sample, on 1,500 samples per label. Those
                                 verdicts are themselves the published result. No script in
                                 this folder produces them, and none may overwrite them.
  Sex, Age                       judged against the ALE curated labels, an external human
                                 curation. The published row covers every applicable curated
                                 record present in the final corpus, so a sample the pipeline
                                 returned as 'Not Specified' counts as a false negative. The
                                 1,500-row files here are a hand-checked subset of that
                                 overlap, not the published row.

Where every benchmark sample carries a gold value, a mismatch counts twice: once as a false
positive for the label the pipeline produced, and once as a false negative for the curated
label it failed to produce. Treatment is the exception, because it has gold-negative
samples, where a wrong label is a false alarm rather than a missed one. The Methods state
this convention; the numbers themselves are reported in the paper and are deliberately not
repeated here.

The files below hold the per-sample record - the GSM, the gold value, the pipeline's
prediction and the verdict - together with the gold sources they were judged against.

Contents
--------
manual_gold_Sex.csv               1,500 GSM; gold, prediction, both parsed, verdict.
manual_gold_Age.csv               1,500 GSM; gold (months), prediction, both parsed to
                                  years, verdict.
manual_gold_Tissue_Condition.csv  1,500 GSM; extracted value, gold value and verdict for
                                  Tissue and Condition.
manual_gold_Treatment.csv         1,500 GSM; extracted value, gold value and verdict.
ale_curated_labels.tsv            the ALE curated labels (Giles et al. 2017, BMC
                                  Bioinformatics 18:509) used as the Sex/Age gold.

No script is shipped to score this benchmark. The verdicts in the files above are the
result: they were assigned by hand, one sample at a time, and there is nothing for code to
recompute. Any script that appeared to regenerate them would only be re-deriving them under
some convention of its own, which is how a published figure drifts away from the judgement
it was supposed to record.

Each manual_gold file holds 1,500 distinct GSM, each occurring exactly once, all present in
the final corpus.

Gold sources shipped here
-------------------------
ale_curated_labels.tsv    the ALE curated labels (Sex, Age).
creeds_disease.json.gz    the CREEDS disease signatures (Tissue, Condition). 828 signatures
                          over 542 GSE, listing 7,974 case samples and 5,077 control samples,
                          13,015 distinct GSM in total; the 1,500-sample benchmark is drawn
                          from that pool. Wang,Z. et al. (2016) Extraction and analysis of
                          signatures from the Gene Expression Omnibus by the crowd. Nature
                          Communications 7:12846.
Every gold column was checked against its source before adjudication: each benchmark GSM
occurs in the source curation, and each gold value agrees with what that source records.

The 924 / 576 split is CREEDS membership, not an assumption: 924 benchmark samples are
listed among a signature's perturbation ids and 576 among its control ids, which is the
stratification Table S17 reports. The verdicts themselves are the authors' own judgement.

Gold standards
--------------
Sex and Age    the ALE label-extraction study's curated labels (Giles et al. 2017), an
               external human curation independent of this pipeline. It is not derived
               from the metadata fields the model reads as input. ALE records age in
               MONTHS; the pipeline reports years.
Tissue and     CREEDS study-context concepts, matched by MeSH concept, synonym,
Condition      morphology and hierarchy.
Treatment      the sample's own GSM/GSE metadata, since no comprehensive curated treatment
               gold exists.

How the Sex and Age sample is drawn
-----------------------------------
The ALE labels cover 38,163 GSM, of which 23,026 are in the final corpus. A sample is
eligible when its value is present (non-"Not Specified") and parses on both sides - the
curated label and the pipeline output - giving 5,806 eligible samples for Sex and 5,105
for Age. From each pool 1,500 are drawn with random.seed(42), and those are the samples
the authors adjudicated. Sex agreement is judged after normalizing to male / female /
mixed; Age is judged as correct when the two values differ by less than one year, after
converting the pipeline's value to years (day/365, week/52, month/12) and the curated
value from months to years.

What the Sex and Age rows do and do not measure
-----------------------------------------------
The published rows are scored over every applicable curated record, not over the
present-in-both subset described above, so their recall is a completeness measurement: it
counts the samples whose record states a sex or an age and which the pipeline nonetheless
left "Not Specified". Had the subset been reported instead, recall would have been 1.000 for
both by construction and would have said nothing about completeness. Tissue, Condition and
Treatment are adjudicated over the full 1,500 including samples the pipeline missed, which
is why Treatment carries TN and FN counts.

Before running anything that reads geo_metadata.sqlite, build it once from the
shipped JSON -- the database is an index over samples_804k.json and is not
shipped twice:
    cd ../../01_input_metadata && python3 build_geo_metadata.py \
        samples_804k.json geo_metadata.sqlite
