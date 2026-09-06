# Calibration (κ) — archived procedure and completed files

Files:
- `calibration_sheet.csv` — 150 stratified dictionary values with completed human `your_verdict` entries
  (per JUDGE_PROMPT.md rubric): OOV rows → `appropriate` / `inappropriate` (+ matched MeSH id if
  inappropriate); MeSH/CVCL rows → `correct` / `incorrect`; `indeterminate` if unsure.
- `judge_verdicts.csv` — the Claude judge's verdicts on the SAME 150 items (kept separate so it does
  not anchor you). Do not open it until you have filled the sheet.

Archived procedure:
1. Fill `your_verdict` in `calibration_sheet.csv` (~15 min).
2. Compare against `judge_verdicts.csv` on the overlapping items → **Cohen's κ** (judge vs you),
   per field and pooled.
3. Interpret (§8): κ ≥ 0.8 → judge reportable with PPI; 0.6–0.79 → usable, wider CIs; < 0.6 → do
   not report judge-derived numbers for that field.
4. If κ is adequate, scale the same prompt to the full §5 stratified sample (head census + tail +
   T5 OOV, ~550 values) and extend to the whole dictionary via PPI++ → the publishable
   inappropriate-OOV / normalization-precision number with valid CIs.

This 150-item set is a **pilot κ** (wide CI). A publishable κ typically needs ~100–200 double-judged
items; expand `calibration_sheet.csv` the same way when ready.
