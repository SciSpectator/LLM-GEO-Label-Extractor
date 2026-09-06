Table S28 - confidence intervals that allow for clustering by study
==================================================================

Wilson's method assumes the 1,500 benchmark samples are independent draws. They are
not: samples within a study share one depositor's wording, so an error on one is
likelier to recur on its siblings, and the interval comes out too narrow.

study_level_metrics.py resamples whole GSEs with replacement (10,000 draws, seed 42) and
recomputes accuracy on each resampled corpus. The columns cluster_boot_lo and
cluster_boot_hi of study_level_metrics.csv are the result; wilson_lo and wilson_hi
are the published intervals, kept alongside for comparison.

Run
---
    python3 study_level_metrics.py

Expected
--------
             accuracy   Wilson              cluster bootstrap    width
Tissue       0.9967     0.9922-0.9986       0.9921-1.0000        1.2x
Condition    0.9500     0.9378-0.9599       0.9113-0.9783        3.0x
Treatment    0.8660     0.8478-0.8823       0.8397-0.8891        1.4x
Sex          0.9987     0.9952-0.9996       0.9956-1.0000        1.0x
Age          0.9860     0.9787-0.9908       0.9604-1.0000        3.3x

The effect is uneven and largest where a field's errors sit in a few studies. The
Condition and Age intervals are about three times wider than reported. These are the
intervals to quote.

The seed is fixed, so the interval reproduces exactly; it is a property of this
benchmark, not an estimate that drifts between runs.
