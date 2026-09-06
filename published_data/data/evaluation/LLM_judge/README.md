# LLM-judge OOV audit

Independent judge = **Claude** (different family from the Gemma-4 pipeline; §8). The exact judge
prompt and reasoning protocol are in `JUDGE_PROMPT.md`. This folder documents the prompt so the
audit is reproducible against any LLM judge; it does **not** contain the chat session.

## Where the judge is applied
Semantic questions deterministic checks cannot answer:
1. **Inappropriate-OOV**: does a MeSH/Cellosaurus term denote an OOV-minted value?
2. Phase-2 assignment precision per tier (planned).
3. Multi-label correctness (planned).
Not applied to abstention or hierarchy granularity (computed from the MeSH graph instead).

## Legacy pilot (`judge_oov_pilot.csv`, 23 stratified OOV values)
- appropriate 13, inappropriate 7, indeterminate 3 → OOV-appropriate precision 13/20 = **0.65** (decided).
- Instance-weighted inappropriate ≈ **37,055** samples (dominated by NSCLC 16,375; PBMC 13,437).
- Key point: 5 of the 7 inappropriate cases (NSCLC, PBMC, Placebo/placebo, AdenoCa) are **missed by
  the deterministic exact-match check** in S7/S14 — confirming that the exact-match figure
  (77/26/18) is a **lower bound** and the true inappropriate-OOV rate is higher.

## Final audit and validity

The final files contain 2,000 judged OOV values and a separate 150-item human calibration.
See `RESULTS.md`. Because the sample deliberately oversampled high-instance values and the
selection probabilities were not retained, 336/1,941 (17.31%) is the unweighted proportion
among decided audit items, not a design-weighted population estimate. Likewise, 32.00% is
occurrence-weighting within the audited items. The deterministic S7/S14 exact-match number
remains the reproducible whole-dictionary floor.
