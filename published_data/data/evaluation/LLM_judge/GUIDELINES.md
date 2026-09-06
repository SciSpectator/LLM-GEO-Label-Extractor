# Annotation guidelines — what to do and which verdict to give

You judge, for each row, whether the pipeline handled the raw value correctly. Fill `your_verdict`
(and `matched_id` only when a term exists that was missed).

## Field scope (which vocabulary applies)
- **Tissue** = anatomy / cell type / **cell line**. Vocabularies: MeSH A-branch + Cellosaurus.
- **Condition** = disease / phenotype. MeSH C or F03.
- **Treatment** = drug / intervention. MeSH D or E02.

## Two row types

### A. OOV rows  (assigned_id starts with `OOV-` or `ART-`)
Ask: **does a controlled-vocabulary term denote this value?**
- `appropriate`   → NO in-branch MeSH descriptor and NO Cellosaurus cell line denotes it → minting
  OOV was the right call. (Compounds with doses, siRNAs, specific mutations, study-specific codes,
  and control states like *normal/control/vehicle/mock* → appropriate.)
- `inappropriate` → a specific MeSH/Cellosaurus term DOES denote it → it should have been mapped.
  **Write that term's id in `matched_id`.** (e.g. NSCLC→D002289, Placebo→D010919.)

### B. MeSH / Cellosaurus rows  (assigned_id starts with `D`, `C`, or `CVCL`)
Ask: **is the assigned concept the right one for the raw value?**
- `correct`   → the descriptor denotes the value (synonyms/plurals count; a slightly broader parent
  is still correct if within ~1 step).
- `incorrect` → wrong concept, wrong branch, or clearly wrong specificity. Put the right id in
  `matched_id` if you know it.

### C. `indeterminate`
Only when you genuinely cannot decide without an external lookup (e.g. an unknown cell-line code).
Use sparingly.

## Principles to keep in mind
1. **Judge the concept, not the string.** A synonym, plural, or common acronym still *denotes* the
   term (NSCLC = Carcinoma, Non-Small-Cell Lung → the OOV is inappropriate).
2. **Control ≠ disease.** normal / control / healthy / untreated / vehicle / mock → appropriate OOV
   (not a MeSH disease). Do NOT map them.
3. **Cell line beats organ.** MCF7 → the MCF-7 cell line is *correct* even if the sample also says
   "breast" — don't mark it incorrect for choosing the line.
4. **Don't penalise field rerouting** or a missing second label — judge only the shown assignment.
5. **Recall failures are inappropriate.** If a real MeSH/CVCL term exists but the value was left OOV,
   that is `inappropriate`, even if the OOV label text looks fine.
6. Ignore trailing qualifiers (stage/grade/dose) — judge the main concept.

## Record
Fill `your_verdict` for all rows; add `matched_id` for `inappropriate`/`incorrect` when you know it.
Then compare to `judge_verdicts.csv` → Cohen's κ (see CALIBRATION.md).
