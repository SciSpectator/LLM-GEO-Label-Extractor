Table S30 - how much of the answer the depositor had already written down
========================================================================

The reviewer read 0.997 F1 on Tissue as implausible against named-entity recognition
state of the art. The two figures describe different tasks, and the difference is
measurable rather than arguable: a GEO record is semi-structured, and much of the
time the answer is already written in a key-value field.

benchmark_grounding.py asks two questions about the INPUT text only, never about correctness:

  verbatim   does the label the pipeline returned occur as text in the sample's own
             record
  keyed      does the record's characteristics field carry a key naming the field
             being extracted (tissue:, cell line:, disease:, treatment: ...)

Accuracy is then read off within each group, from the unchanged Table S1 verdicts.

Run
---
    python3 benchmark_grounding.py

Expected
--------
           verbatim   keyed  acc|keyed   not keyed  acc|not keyed
Tissue      78.3%      662     0.9924        838       1.0000
Condition   71.8%      462     1.0000      1,038       0.9277
Treatment   40.5%      249     0.9518      1,251       0.8489

A GEO record states the tissue outright four times in five and the treatment two
times in five, and Treatment accuracy falls from 0.9518 to 0.8489 when no key names
the field. The same model on the same corpus loses ten points where the metadata
stops cooperating. That spread, not a claim to beat entity recognition on prose, is
what separates the Tissue and Treatment rows of Table S1.

The script also prints how 'Not Specified' is scored per field, and the sensitivity
of Condition to the one scoring rule a reader cannot infer: 25 samples whose CREEDS
gold is 'control' were accepted with an empty Condition. Scoring those as misses
gives Condition 0.9635 / 0.9333 / 0.9482 against the published 0.9641 / 0.9500 /
0.9570.
