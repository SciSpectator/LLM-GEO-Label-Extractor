Table S29 - rare against common concepts
========================================

The reviewer asked for macro averaging specifically to see how the pipeline does on
rarer classes. A sample-weighted average is carried by whichever concepts happen to
be frequent in the benchmark; averaging over the distinct gold concepts gives a
tissue or disease seen twice the same weight as one seen sixty times.

concept_level_metrics.py groups the Table S1 verdicts by their gold concept and reports
both averages, plus the two tails: concepts appearing at most twice, and concepts
appearing more than eight times.

Treatment has no row. Its gold is the sample's own metadata, adjudicated case by
case, so there is no class to group by - inventing one would be a different
measurement wearing this one's name.

Run
---
    python3 concept_level_metrics.py

Expected
--------
Tissue     254 concepts (56 seen once)   micro 0.9967   macro 0.9959   rare 1.0000
Condition  154 concepts (28 seen once)   micro 0.9500   macro 0.9260   rare 0.8750

Condition is the finding: the concept-weighted average is 2.4 points below the
sample-weighted one, and the rarest concepts are answered at 0.875 against 0.949 for
the commonest. Rare diseases are genuinely harder and the headline figure hides it.
Tissue shows no such gap.
