# What this bundle is, and how it differs from the released code

The released pipeline is `Paper_Data/code/`. This bundle is that code plus a
bounded set of repairs, and the point of this file is that the boundary is
checkable rather than asserted: every file is either byte-identical to
`Paper_Data` or listed below with what changed and why.

Verify it directly:

```bash
diff -rq Paper_Data/code/1_extraction_phase1_1b pipeline/phase1
diff -rq Paper_Data/code/2_normalization_phase2 pipeline/phase2
```

## Phase 1 — unchanged

All 15 files are byte-identical to `Paper_Data`. Extraction was not modified.
This matters for reading the re-extraction results: where a re-extracted sample
now carries a different label, the cause is Phase 2, the assembly step, or the
model's own sampling variance — not a changed extraction prompt.

## Phase 2 — 14 files unchanged, 6 repaired, 3 added

| File | Δ lines | What it fixes |
|---|---|---|
| `cellline_db.py` | +107 / −37 | `LIMIT 1` returned whichever catalogue row was read first for an ambiguous name. Adds exact-unambiguous matching, a species filter, and `donor_sex`, so the catalogue's own attributes travel with the identifier. |
| `phase2_mesh.py` | +120 / −29 | Short-form stage: consult the study's own definition first; accept a model expansion only if the resolved concept spells the abbreviation. Adds the pure-qualifier guard at tier 0. |
| `build_index.py` | +99 / −9 | Surface forms mapping to more than one identifier are withheld from the index instead of silently resolving to the first. Writes `.collisions.tsv`. 539,100 forms indexed, 5,642 withheld. |
| `build_cellosaurus_db.py` | +36 / −19 | Carries `organism` and `donor_sex` into the database so downstream contradiction checks are possible at all. |
| `mesh_lookup.py` | +27 / −0 | `forms_of()`: every name a concept carries, so a concept is not judged through one editorial heading. |
| `phase2.py` | +26 / −4 | Collects up to six sample snippets per (acronym, study) so the short-form stage sees the samples that actually carry the value. |

Added:

| File | Lines | Why it is not in `Paper_Data` |
|---|---|---|
| `acronym_expand.py` | 452 | Deterministic definition extraction (Schwartz & Hearst initial-letter alignment) and the expansion contract. The released short-form stage had a prompt/parser mismatch that silently returned nothing. |
| `qualifier_guard.py` | 126 | Recognises a value made only of stage/grade scaffolding, which must not be resolved as a disease name. |
| `build_vocab_from_meshdb.py` | 84 | Build tool, not pipeline. `Paper_Data/build_vocab.py` reads the MeSH XML release, which the package does not ship; this produces the same `vocab` table from `mesh.sqlite`. |

## Assembly

`merge_final_labels.py` gains two reconciliations, both at the only point in the
pipeline where the contradicting facts coexist: Phase 1 infers Sex and never sees
a catalogue resolution, Phase 2 resolves the catalogue and never sees Sex.

* attested donor sex of a catalogued line outranks an inferred Sex (571 samples);
* sex-specific anatomy outranks an inferred Sex (11 samples), with the
  maternal–fetal interface excluded via the MeSH `A16` branch.

## Reference data

`reference/mesh.sqlite` is the released database and carries tables written by
earlier runs (`resolutions`, `verifier_decisions`, `polarity_decisions`,
`oov_mesh_clusters`, `oov_mesh_synonyms`, and two `alien_*` tables left by an
April 2026 build that no current code reads). Every job copies the file and
clears all of them before starting: a normalisation that inherits the previous
run's decisions is not a new normalisation, and the OOV clusters in particular
encode the mistakes being repaired.

## Runs

| Job | Scope | Result |
|---|---|---|
| A | Acronym instances the release left unresolved; unchanged Phase 1 output, repaired Phase 2 | see `JOBS/A_acronyms/out` |
| B | The 6,047 samples flagged by the six integrity screens, re-extracted from raw GEO records (Phase 1 + Phase 2) | 6,026 cleared; 21 remain, all screen exemptions; 0 genuine errors |

Neither job re-runs the 804,427-sample corpus, so the corpus-wide figures in the
manuscript are unchanged and remain those of the released run.
