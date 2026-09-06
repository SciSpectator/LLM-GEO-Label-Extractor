# Architecture, models, reasoning, and memory

This document is the *complete* connected picture: every stage, every per-label
worker, every box in the data flow, which model runs it, whether reasoning is on
or off, and the guard rules that make the output faithful to each sample's own
evidence. It is deliberately detailed. For the short version, read the
[README architecture section](../README.md#architecture).

The desktop GUI and the command-line interface are two front ends to the same
`geo-label-extractor` orchestrator (`geo_pipeline.py`). They do not implement
separate pipelines. Every run writes one output directory containing the selected
input manifest, per-stage checkpoints, the GSE context cache, the normalization
dictionary, audit records, completion markers, and the final exports.

[![Pipeline architecture](../assets/architecture.svg)](../assets/architecture.pdf)

[Open the PDF](../assets/architecture.pdf) to read the diagram at full size.

## Model assignments

| Stage / box | Default served model | Precision & decoding | Reasoning | Persistent state |
|---|---|---|---|---|
| Phase 1, Tissue/Condition/Treatment/Sex | `google/gemma-4-12b-it` | FP8 weights, 16-bit KV cache, 8,192-token context, temperature 0, seed 42, `num_predict` 256 | **off**, grounded verbatim span copy | input-hash decision cache |
| Phase 1, Age | `google/gemma-4-e2b-it` (separately set via `AGE_MODEL`) | BF16, temperature 0, seed 42, `num_predict` 1024 | **on**, for units, coded ages, life-stages, timepoint disambiguation | same Phase-1 cache, keyed with the Age model id |
| Phase 1, `resolve_coded_value` | Phase-1 model | deterministic decoding | off | (folds into the Phase-1 result) |
| Phase 1b, GSE context recovery | `google/gemma-4-12b-it` | same endpoint, deterministic decoding, `LLM_NUM_CTX` 8,192 | **off**, bounded recovery | SQLite GSE context cache (per-`(GSE,GSM,field)`) |
| Phase 2, entity-linking cascade | `google/gemma-4-e2b-it` | BF16, deterministic decoding, JSON-schema-constrained | **on**, for candidate selection, OOV, curation | per-value checkpoint + final dictionary |
| Phase 2, retrieval / consolidation | BioLORD-2023, SapBERT (embeddings) | dense vectors, cosine | n/a | prebuilt vocab index (`.npz`) |
| Final assembly | none, deterministic Python | linear pass over exported tables | n/a | `manifest.json` |

Model identifiers are served-model names. For an archival run, replace them with
the exact repository revision or server digest the operator used. Reasoning is
controlled per stage by the `PHASE1_THINK` / `PHASE1B_THINK` / `PHASE2_THINK`
environment variables (`run_cli.py` sets Phase 1/1b off and Phase 2 on by
default, and `--no-think` forces reasoning off everywhere).

## Connected staging

The `geo-label-extractor` driver runs three sequential stages, `extract`,
`normalize` and `assemble`, that share one run directory, one checkpoint format,
and one completion-marker scheme, so a single command runs the whole pipeline or
resumes at any stage (`--from-stage {extract,normalize,assemble}`,
`--no-assemble`, `--force`). The FP8 12B and BF16 e2B models do not fit in one
node's memory at the same time, so if memory is tight, stop after extraction,
swap the served model, and resume at `normalize`.


---

# Stage 1, extraction (`run_cli.py`)

Extraction is the only stage that reads GEO metadata and calls the extraction
model. Its output is one JSON shard per platform, each sample carrying three
nested dicts, `phase1`, `phase1b` and `phase2`, plus the raw fields it was scored
against.

## Driver behaviour (the boxes around the model)

- **Selection → samples.** Samples arrive as a GSM manifest, a GPL dump from
  `GEOmetadb.sqlite` (`_dump_gpl_samples` reads `title`, `source_name_ch1/2`,
  `characteristics_ch1/2`, `treatment_protocol_ch1/2`, `description`,
  `series_id`), or an LLM-planned catalogue search (`selection.py`).
- **Channel merge (`_chan`).** Every `<field>_chN` present on a record is merged
  in numeric order, distinct non-empty values joined with `" | "`, so two-colour
  arrays are consumed in full rather than reading only ch1.
- **Group by GSE.** Samples are bucketed by series, because Phase 1b and the
  Phase 2 warm-up both operate per series. The **resume unit is the GSM**, not
  the GSE: one unextractable sample never discards its ~50 siblings, and a series
  that is 90% done is topped up rather than restarted (`_read_checkpoint` keeps
  the last record per GSM).
- **GSE metadata scrape.** For each new series, `scrape_gse_meta` pulls the NCBI
  SOFT record and keeps `!Series_title`, `!Series_summary`,
  `!Series_overall_design`. Results are cached to `gse_meta.json`, and `--no-scrape`
  reuses the sidecar. `get_or_build_compressed` compresses the summary to ~512
  chars per series for the Phase 1b prompt.
- **Escalation (two-pass input).** `_build_raw(extended=False)` first shows the
  model **only** title + source + characteristics. Only labels that come back
  `Not Specified` are retried with `extended=True`, which adds
  `treatment_protocol` and `description`. Unconditional extra text was measured
  to *cost* accuracy on samples whose answer was already present (−3.2pp Age,
  −2.0pp Condition), so the extra fields are shown only on a miss.
- **Health gate.** Per-phase error fractions are tracked, and if any phase exceeds
  25% error the run aborts loudly (inference-server failure / GPU OOM / DB
  corruption) rather than shipping a degraded corpus. A `_P1_FAILED` sample is
  dropped unwritten and retried on resume.

## Phase 1, verbatim per-label extraction (12B FP8, reasoning off)

`Phase1Extractor.extract` fans the sample out to up to five per-label workers in
parallel (`LABEL_COL_WORKERS`, default 3, over a process-wide `label` pool). Each
worker is an independent DSPy-style signature with its own compiled prompt (or a
verbatim fallback prompt shipped in `phase1.py`), rendered in the
`[[ ## field ## ]]` chat layout, decoded at temperature 0 / seed 42.

Every worker's north star is the same: **copy a span that literally appears in
THIS sample's metadata, never generate, never infer from world knowledge, never
borrow from the series.**

### Tissue box, `extract_tissue`
Walks a fixed source hierarchy and stops at the first non-empty source:
`cell line:` → `cell type:` → `tissue:` → `organ:` / `anatomical site:` /
`biopsy site:` → `source_name` → title/description (last resort). For a compound
`<Organ> Cancer/Carcinoma/...` it returns the **organ noun alone** when an
explicit organ noun is present, but keeps abbreviated cancers (OSCC, HCC, TNBC)
and cell-line IDs whole. Output passes through `resolve_coded_value` then the
verbatim guard.

### Condition box, `extract_condition`
Recognises **three equal-priority** categories: (1) disease names / phenotypes /
stage-grade-severity markers, (2) an **explicit healthy / control / normal**
sample state, emitted verbatim, since a documented control is *not*
`Not Specified`, and (3) bare pathological markers (`tumor`, `lesion`, `metastasis`). It is
**this-sample scoped**: the GSE topic alone is not evidence, so a control sample
in a disease study yields its control state, not the study disease. Then three
deterministic post-checks fire:
- **Field-level denial** (`_is_denied_field_value`): a disease-named field with a
  denial value (`N`, `No`, `0`, `false`, `negative`, `absent`, `unaffected`) →
  `Not Specified`.
- **Cell-line-as-Condition recovery** (`_condition_cellline_recover`): if the
  value is *only* a code recognised in **Cellosaurus** (not a regex, so `T2D`
  is never mistaken for a line), it is replaced by the disease phrase stated in
  the sample's own text, or `Not Specified`.
- Verbatim guard.

### Treatment box, `extract_treatment`
Decides on **what was administered**, not on whether the sample was the
comparison arm. An inert carrier / vehicle / placebo / empty vector / `siControl`
that *was given* is a Treatment, whereas a bare `control` / `0Gy` / `no drug`
names nothing given and is `Not Specified`. Two hard guards apply: **no
world-knowledge drug inference** from a cell line's or disease's known clinical
use, and the **shared-protocol control-arm guard**, where a `treatment_protocol` describing the
whole experiment is not assigned to a sample whose own text marks it a control /
vehicle / mock / vector / wild-type / naive arm. A **universal definition**
overrides narrower phrasing: assay-protocol text (bisulfite conversion,
hybridisation, library construction, kit/reagent/instrument names) is *never* a
Treatment even inside a field literally named `treatment_protocol`.

### Sex box, `extract_sex` (demographic)
A **fast deterministic path** first (`_fast_sex`): scans `characteristics`
key/value pairs whose key contains `sex`/`gender`, maps `m/male/man/boy` and
`f/female/woman/girl`, and returns `_DEFER` to the reasoning LLM only when the
field embeds a legend (`(`, `[`, `=`) or the tokens conflict. The LLM path adds
**structural legend decoding** (a code-to-meaning key embedded in the field name
is read left-to-right and the paired word emitted, never the raw code) and two
refusals: a **bare numeric code with no in-sample legend** → `Not Specified`
(never guess which number means which sex), and **never infer sex from biology**
(a sex-linked disease/organ/hormone is not a sex statement). Finally
`_ground_sex` requires an actual sex token somewhere in the raw fields
(`_sex_grounded`), and an ungrounded `male`/`female` is demoted to `Not Specified`.

### Age box, `extract_age` (e2B, **reasoning ON**)
Age is the one Phase-1 field run on the separately configured reasoning model
(`AGE_MODEL`, `think=True`, `num_predict` 1024) because it requires normalising
heterogeneous phrasings, not copying a span. It emits the **first form that
fits**: **Form A** `"<unit>: <number>"` with unit ∈ {years, months, weeks, days}
(unit token on the number → unit named in the field → bare number under an age
field = years, postnatal `day N`/`P N`/`PND N` → days, spelled-out numbers →
digits, approximate markers dropped and number kept). **Form B** is
`"age: <expression>"`,
kept verbatim for anything Form A cannot hold (comparator bounds `<1.5 years`,
gestational/embryonic shorthand `GW22`, `E14.5`, qualitative life stages
`neonate`, `juvenile`, `elderly`). It uses **THIS subject's own age only** (never
a maternal/paternal/donor age in a sibling field), and returns `Not Specified`
for a lone code under an `age group` field with no stated age. Downstream, the
Phase 2 export parses that unit form into the separate `final_Age_value` and
`final_Age_unit` columns using `_AGE_UNIT_MAP`, so identical ages stop looking
like disagreements.

### `resolve_coded_value` box (legend decode, T/C/T only)
After a raw span is extracted, if it contains a coded token this worker consults
a **legend present in the same sample's metadata** and substitutes the meaning:
explicit `X = meaning` definitions, **self-legending field names** where the
field name *is* the condition and the value is a polarity axis (affirmative →
emit the condition, negative → `Not Specified`), and in-field parentheticals
`RRMS (Relapsing Remitting Multiple Sclerosis)`. It returns the value
byte-identical when no legend applies.

### Verbatim-enforcement guard (`_verbatim_enforce`)
The final Phase-1 gate on T/C/T. A `;`-separated part survives only if it is a
spacing/punctuation-insensitive substring of the metadata **or** every content
(non-stopword) token appears in the source. This keeps faithful dose spans
(`5 mg/kg` vs `5mg/kg`) while dropping a fabricated word riding along on grounded
ones. Sex and Age are demographics and bypass this guard (they have their own
grounding checks).

## Phase 1b, GSE context recovery

This is the *context-aware* heart of the pipeline: it recovers Tissue /
Condition / Treatment that Phase 1 left `Not Specified`, using series-level
evidence, **without ever overriding a grounded per-sample value**. It runs on the
12B model with **reasoning off** (a bounded recovery step).

**Why it exists.** A GSM is often not self-contained: its own `title`,
`source_name`, and `characteristics` may say only `"sample 7"`, `"shCtrl_rep2"`,
or a bare cell-line code, while the study's disease-vs-normal contrast, control
arm, or common tissue lives only in the series-level text and in the pattern of
its sibling samples. Phase 1b supplies that missing context from the *series* the
sample belongs to, while explicit per-sample evidence always wins.

**Per-GSE agents with KV reuse.** `Phase1bRecovery` keeps one `GSEInferencer` per
series. Each inferencer holds three independent system prompts, one per label
column, with **this** series' title/summary/design baked in, so the inference
server reuses the system-prompt KV tensors across every sample in the series
(~40% faster). A sample is skipped entirely when it has no `Not Specified`
T/C/T fields.

**Two evidence sources per call.** For each still-missing column the agent
passes: (1) THIS sample's own title/source/characteristics + its Phase-1 value,
and (2) the **sibling distribution**, a `Counter` of what the *other* samples in
the series got for that column, with this sample's own value decremented out,
formatted as `"value" (n=…)` cohort evidence. For Tissue it additionally shows
the sample's Phase-1 Condition and Treatment (so a disease can imply an organ).

**Refinement mode** (fires when Phase 1 *did* return a value): run structural
checks in order and apply the first that fires, else emit the Phase-1 value
verbatim. **Check 1, acronym expansion**: an all-caps token whose letters match
the initials of an N-word phrase in the sample or GSE text is replaced by that
phrase (initials only, with no prior knowledge of what the acronym "usually"
means). **Check 2, fragment to fuller phrase**: a longer phrase in the supplied
text that literally contains the Phase-1 value and adds a head noun or
anatomical qualifier replaces it. The replacement always preserves the same
entity and never introduces a different disease, organ, or compound.

**Per-column guards** (structural, no entity hardcoding):
- **Tissue**, where "disease implies organ" is a **last-resort fallback**, applied
  only when the sample text is fully silent about biological source *and* the
  GSE/siblings pin no shared tissue *and* the Phase-1 Condition names an
  anatomically-defined disease. It emits the implied organ, never for
  systemic/metabolic/hematologic diseases, and never when the material was drawn
  from a surrogate site.
- **Condition**, where **control never replaces a stated disease** (highest
  priority): a diseased sample that *also* carries a treatment-arm control marker
  keeps the **disease**, because the control marker describes the treatment arm,
  not the disease status. A genuine *subject* control (healthy donor, `disease state: normal`)
  still yields `healthy`/`control`. Relational disease mentions (`sibling of…`,
  `donor for…`) belong to the proband, not this sample. An explicit
  `diagnosis:` / `disease state:` / `affection_status:` field wins.
- **Treatment**, with a **control-leak guard**: the shared protocol and GSE context
  describe the treated arms, so a sample whose own text marks it control/vehicle/
  vector/wild-type/naive is *not* assigned the active drug. It short-circuits
  (returns the Phase-1 value) before the call when the sample's own text already
  reads as a control.

**Output guards after the call**: an echoed prompt fragment
(`_ECHO_MARKER`) is discarded, a Condition that came back as a bare cell-line
code is repaired to the sample's own disease phrase, an ungrounded Treatment
(`_treat_grounded`, meaning no content token in the sample's own text) is
reverted to the Phase-1 value, and a bare-control Condition is upgraded to a
recovered disease when one exists. Anything still `Not Specified` stays `Not Specified`.

## Sex / Age pass-through and post-format

Sex and Age are **demographics-only**: Phase 1b and Phase 2 do not run on them
(GSE context cannot infer a per-subject demographic, and MeSH normalisation does
not apply). They pass through verbatim, with `sex_normalize.normalize_sex`
canonicalising the surface form and the Phase 2 export splitting the Age unit
form into value and unit columns. Every Phase-1b value is written to the GSE context cache
(`upsert_phase_value`) keyed `(GSE, GSM, field, p1b)`.

## Phase 2 warm-up inside extraction

When Phase 2 runs inside `run_cli.py` (the in-process `Coordinator`, distinct
from the batch `run_phase2.py` normalizer), each series first seeds its unique
`(raw, column)` pairs into a per-GSE sibling cache before the per-sample collapse
calls, so context-dependent short forms resolve consistently within a series. The
shipped unified pipeline uses the batch normalizer below, and the in-process path
is retained for single-series interactive runs.

---

# Stage 2, entity linking (`run_phase2.py`)

Normalization builds **one dictionary** over the whole corpus: every *distinct*
raw Tissue/Condition/Treatment string is decided **once** and applied everywhere
it occurs, so the corpus is internally consistent by construction. The
`_assert_one_answer_per_label` invariant fails the run loudly if any label ever
carries more than one answer. The Phase-2 model is the e2B reasoning model.

Resolution order is `Phase2Mesh`'s. **Field scope is enforced throughout**:
Tissue may hold anatomy, cell types and cell lines, Condition may hold diseases,
and Treatment may hold drugs, biologics and therapeutic procedures. A label filed
under a field that cannot hold it is removed from that field, and one with an
unambiguous target is moved to the field that can.

The ordered cascade, box by box:

1. **Build dictionary.** Collect distinct values and per-value instance counts
   from the detected phase-1 stage (`phase1b` preferred over `phase1`, chosen from
   the corpus, never assumed). Short forms are recorded per study.
2. **Episodic recall.** A `(value, column)` already decided in this run is
   returned from memory rather than re-decided.
3. **Polarity gate.** Asserted entities are separated from negated or absent
   states, so "disease-free" is not resolved as the disease. Configurable through
   `PHASE2_USE_POLARITY`, on by default, which is the published configuration.
4. **Cellosaurus gate (Tissue only).** A deterministic catalogue match, guarded
   by an LLM cell-line identity check, assigns established lines their CVCL
   identifier. Each catalogued entry carries its organism and donor sex, so a
   contradiction between an inferred attribute and an attested one is detectable
   rather than silent.
5. **Exact MeSH** under the field scope above.
6. **Qualifier guard** (`qualifier_guard.py`). A value composed only of stage or
   grade scaffolding is not treated as a disease name.
7. **Existing out-of-vocabulary lookup.** A concept already minted for this
   surface is reused.
8. **Short-form expansion, per study** (`acronym_expand.py`). The same few
   letters mean different things in different experiments, so each
   `(value, GSE)` short form is resolved against **its own** study context. The
   study is consulted first: where it defines its abbreviation in the standard
   parenthetical form, the expansion is extracted by initial-letter alignment and
   **quoted rather than inferred**. The model is consulted only where the study
   is silent, and a model-proposed expansion is accepted only if the concept it
   resolves to actually spells the abbreviation, tested across every name that
   concept carries. This rejects the characteristic failure in which a study's
   subject is returned in place of the abbreviation's meaning.
9. **Candidate retrieval.** For every pending value, **BioLORD-2023** returns the
   top-*k* controlled-vocabulary candidates (MeSH descriptors/entry-terms/
   supplementary concepts + Cellosaurus lines) from the prebuilt index. A surface
   form mapping to more than one catalogue identifier is **withheld from the
   index** rather than resolved to whichever entry is read first.
10. **Constrained selection with a keep/reject verifier** (`normalize_one`,
    reasoning on) under locked rules. The model picks the candidate that denotes
    the concept, or declares it out-of-vocabulary. With `--escalate`,
    low-confidence / unresolved values are re-decided at a larger reasoning
    budget. Configurable through `PHASE2_USE_PICKER`, on by default.
11. **Minting and vocabulary recovery.** Values with no faithful vocabulary term
    are minted as out-of-vocabulary concepts, and values that carry a Cellosaurus
    or MeSH id the selector missed are re-confirmed against just those candidates.
12. **OOV consolidation** (`_mint_by_concept`, BioLORD-assisted). Uncovered
   concepts are given **one local identifier per concept, however many ways it is
   spelled.** Labels are processed most-frequent-first, and each is compared (dense
   similarity shortlist → LLM `assign_concept`) against the concepts already
   recorded and either folds into one or starts a new one. Sequential processing
   makes consolidation **transitive** (a fourth spelling joins the concept its
   predecessors built) and independent of array ordering.
13. **Curation review** (`phase2_curate`), the stage that produces the labels
   that *ship*, and the mechanism that **relocates and removes** mis-assigned
   labels:
   - **Pass A, value review, confirm-before-change.** Every value is re-judged
     (`_SYS_ASSIGNED` / `_SYS_UNASSIGNED`). *Keeping is free, while changing
     requires a second, independent review at a larger reasoning budget that
     reaches the same conclusion*, so an unconfirmed change is discarded.
     **Effort follows impact**: a value used in ≥50 samples is always re-reviewed,
     and a value used once is reviewed once unless its first reviewer wanted a
     change. Actions are
     `KEEP` / `REPLACE` (move to a better candidate, a **relocation**) /
     `ASSIGN` / `NONE` (the label asserts nothing → `Not Specified`) / `WRONG`
     (flag, keep, do not guess).
   - **Pass B, cluster consistency.** Every label reaching the same target is
     examined together, and a member that denotes a different concept is flagged
     (`cluster-outlier`) and **removed** from that cluster, again only when a
     second review confirms. This catches individually-plausible assignments that
     are mutually inconsistent.
   - Values reviewed out are re-minted as their own concepts, and a
     curation-failure report records anything left unreviewed.
14. **Closing re-check.** Several stages choose the term they emit independently
    of the term the vocabulary was asked about, so every minted out-of-vocabulary
    identifier is re-tested against the surface it carries and replaced with the
    descriptor wherever the vocabulary does hold one.
15. **Apply + export.** The finished dictionary is applied to every shard as pure
    lookup, and `phase2_export.export` writes **one gzipped CSV per platform** to
    `<out>/normalized/final/<GPL>/<GPL>.csv.gz`. `phase2_export.header()` already
    emits all five `final_*` labels (`final_Sex`, `final_Age`, `final_Tissue` +
    `_id`, `final_Condition` + `_id`, `final_Treatment` + `_id`).
16. **Case fold** (`phase2_casefold.py`), the first of two deterministic steps
    that close the phase. The cascade mints one concept per surface form, so the
    same concept can fragment across ids differing only in case, whitespace,
    hyphen, underscore or dot (`PBMC`/`pbmc`, `wild type`/`wildtype`). Every such
    group is folded onto one canonical id and label, the most frequent surface
    form. Only out-of-vocabulary assignments are touched, and MeSH descriptors and
    Cellosaurus identifiers are never changed. The orchestrator runs this
    automatically after normalization.

The semantic models (BioLORD, SapBERT, the e2B selector) only *shortlist* or
*consolidate*, and they never bypass the evidence hierarchy or mint fake MeSH
identifiers.

---

# Stage 3, final assembly (`final_assembly.py`)

The deterministic, model-free step that turns the per-platform Phase 2 exports
into the corpus that ships.

- **Merge platforms.** Concatenates every `<out>/normalized/final/<GPL>/<GPL>.csv.gz`
  into one master corpus, asserting that all platform headers match so no column
  silently misaligns.
- **Duplicate-id relocation** (`deduplicate_ids`). Within a `;`-joined cell, a
  repeated `(label, identifier)` pair has the duplicate dropped, keeping the
  first, the corpus-level counterpart of Pass B's cluster cleanup.
- **Within-sample de-duplication** (`phase2_dedup.py`), the second deterministic
  step closing Phase 2. Phase 1 extracts one or more spans per field and Phase 2
  normalises each separately, so two spans of one sample that denote the same
  concept leave a repeated identifier in that sample's multi-label cell. Repeated
  *identical* ids are collapsed to a single occurrence, first-occurrence order
  preserved. Distinct ids, including a Cellosaurus id beside a MeSH one, and
  `Not Specified` entries are left untouched, so no concept is lost. No model and
  no Phase 2 re-run are needed. The orchestrator runs this automatically over the
  merged corpus.
- **Outputs.** `<out>/final_labels/LLM_labels_all_samples.csv.gz`, a per-platform
  mirror under `by_GPL/`, and `manifest.json` (samples, platforms, `sex_present`,
  `age_present`, `relocated_cells`).

The GUI's "Full pipeline" runs `--stop-after phase2`, which now auto-runs this
assembly. `--no-assemble` stops after normalization, and `--from-stage assemble`
rebuilds only the merged corpus from existing Phase-2 exports.

---

## Cache and persistent memory

"Memory" here means deterministic, inspectable local state, not hidden
conversational memory:

- **Phase-1 decision cache** keys results by raw-input hash, field, model, and
  prompt version (`PHASE1_PROMPT_VERSION`, with the Age worker's key carrying the
  Age model id). A model or prompt change cannot silently reuse an old answer.
- **GSE context cache** is SQLite keyed by `(GSE, GSM, field)`, storing Phase-1
  values, Phase-1b values, raw context, the compressed series summary, and
  sibling-label aggregates.
- **Phase-2 decision checkpoint** stores one decision per unique raw label (plus
  per-study short-form expansions) so repeated strings cannot receive different
  targets, and a resumed run reproduces a one-shot run.
- **Final dictionary** (`phase2_dictionary.json.gz`) maps every raw
  Tissue/Condition/Treatment value to its canonical name, identifier, source
  tier, frequency, stage, and curation provenance.
- **Completion markers** (`*.done`) permit safe resume and stop a finished stage
  from being repeated unintentionally.

No model response is learned into weights. Caches can be deleted to force a clean
run, or archived with checksums to reproduce a published run.

## Reasoning budget summary

| Stage / box | Reasoning | Why |
|---|---|---|
| Phase 1 T/C/T/Sex | off | verbatim span copy, since reasoning invites fabrication |
| Phase 1 Age | on | heterogeneous phrasings must be normalised, not copied |
| Phase 1b | off | bounded recovery under strict guards |
| Phase 2 selection / OOV | on | judging concept identity among constrained candidates |
| Phase 2 curation (confirm pass) | on, larger budget | an independent second opinion before any change ships |
| Final assembly | none | pure deterministic merge and relocation |
