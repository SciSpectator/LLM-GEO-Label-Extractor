Table S23. Integrity screens. Six deterministic screens over the corpus, each
testing one pair of fields for a contradiction that neither field can show on
its own: a cell line whose catalogued organism is not human; a cell line whose
attested donor sex contradicts the extracted Sex; a Condition consisting only of
stage or grade scaffolding; an Age whose unit was dropped, leaving the value in
the 1-12 year band; Sex contradicting a sex-specific condition; and Sex
contradicting sex-specific anatomy.

Result files in this folder:
  screen_verification.csv   per screen: samples flagged, cleared, still flagged
  still_failing.csv         one row per sample that still trips a screen, with
                            the reason it is exempt or GENUINE if it is not

Generating script in this folder: verify_screens.py

Run:
  PYTHONPATH=<phase2_code_dir> AGE_MODULE=<assembly>/age_composition.py \
  python3 verify_screens.py <labels.csv.gz> <mesh.sqlite> <cellosaurus.sqlite> \
                            <evidence_dir> <out_dir>

The labels file must be the assembled five-label corpus rather than the Phase 2
output, because Sex is reconciled against the cell-line catalogue and against
sex-specific anatomy at assembly time, and two of the screens read that
reconciled value.

A residual is classified rather than merely counted. The MeSH tree places
germ-cell and embryonal neoplasms under both pregnancy complications and
testicular tumours, so a male sample carrying one is not a contradiction; nor is
a placental or cord-blood sample carrying the mother's diagnosis. Those two
exemptions are read from the tree, not from a list. Anything else is reported as
GENUINE, and the script exits non-zero when that count is not zero, so a run
that acquires a real contradiction fails rather than passing quietly.

Inputs shipped for this table:
  evidence/                        the six flagged-sample lists the screens read
  ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz
  ../../../reference/mesh.sqlite
  ../../../reference/cellosaurus_repaired.sqlite   (carries organism and
      donor_sex; the released cellosaurus.sqlite has neither, and two of the
      screens cannot run without them)
  qualifier_guard.py, age_composition.py           (imported by the script)

Run:
  PYTHONPATH=. AGE_MODULE=./age_composition.py python3 verify_screens.py \
    ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz \
    ../../../reference/mesh.sqlite ../../../reference/cellosaurus_repaired.sqlite \
    ./evidence <out_dir>
