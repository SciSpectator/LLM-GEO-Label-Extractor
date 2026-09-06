# Methods and theory

## Problem formulation

GEO sample annotations are depositor-authored free text. The same concept can be
written in many ways, important evidence may be split between GSM and GSE
records, and absence of evidence must not be converted into a positive claim.
The pipeline therefore separates **extraction** (what the record says) from
**normalization** (which controlled concept represents that statement).

## Phase 1: evidence-grounded extraction

Each GSM is represented by its title, source, characteristics, treatment
protocol, and description. Field-specific prompts request one output for Sex,
Age, Tissue, Condition, and Treatment. Tissue/Condition/Treatment outputs pass a
verbatim-grounding guard: content words absent from the input are rejected.
Sex is accepted only when an explicit sex/gender token is present. Age is run
through the separately configured reasoning-capable model because units, coded
values, developmental ages, and experimental time points require disambiguation.

The released defaults use temperature 0, seed 42, an 8,192-token context, and a
short answer budget for extraction. `Not Specified` is a deliberate abstention,
not a negative biological finding.

## Phase 1b: bounded GSE-context recovery

Only unresolved or structurally incomplete Tissue, Condition, and Treatment
labels are reconsidered. The model receives GSE title/summary/design plus the
distribution of sibling GSM labels. An explicit GSM value cannot be overwritten
by contextual inference. Generic GSE topic text alone is insufficient evidence
that every sample has a disease or received a treatment. Study arms and
control-state language are preserved.

## Phase 2: controlled-vocabulary entity linking

Normalization operates on the global dictionary of unique extracted strings,
not independently on 804,427 samples. One raw value is therefore resolved once
and reused, improving consistency and cost.

The ordered cascade is:

1. persistent prior-decision cache
2. `Not Specified`, polarity, and field-routing guards
3. exact Cellosaurus identity match for established cell lines
4. exact MeSH descriptor/entry-term/supplementary-concept match
5. BioLORD-2023 semantic candidate retrieval
6. reasoning-model candidate selection with field constraints
7. OOV concept creation when no controlled-vocabulary candidate is adequate
8. SapBERT-assisted OOV consolidation
9. deterministic final case-fold/spacing consolidation
10. dictionary application to every GSM with audit provenance

Cell-line identity is not replaced by a disease or tissue merely because such a
concept is semantically nearby. Control, healthy, normal, vehicle, and sham
states remain distinct by design. OOV identifiers are local identifiers and are
never presented as fabricated MeSH identifiers.

## Determinism and checkpoints

The LLM calls use deterministic decoding settings, but server kernels and model
revisions can still affect outputs. Reproducibility therefore relies on pinned
model revisions plus the released decision dictionary and audit logs.
Extraction checkpoints are per sample/GSE, and normalization checkpoints are per
unique raw value. Restarting resumes completed work.
