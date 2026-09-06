Error analysis — what is still wrong in the corpus the paper reports.

Every count here is taken on the released corpus after the corrective
re-extraction, so these are the errors that survive rather than the ones that
were repaired. The Error analysis section of the main text is written from these
files.

Result files in this folder:
  error_classes.csv            one row per (label, error class): samples,
                               % of that label's extracted samples, cause, note.
                               Genuine errors only.
  not_errors.csv               counts that show up in a check but are correct
                               outputs, kept out of error_classes.csv so nothing
                               can sum them into an error total
  field_scope_corrections.csv  values removed from or moved between fields
  acronym_residue.csv          what share of acronym-shaped Condition labels was
                               resolved, and for the rest, why not (Table S26)
  acronym_audit.csv            one row per unresolved acronym-and-study pair:
                               the expansion found in that study's own text, the
                               verdict and the MeSH id where one exists
  acronym_audit_summary.csv    the same rolled up, with the share of unresolved
                               occurrences and the share of the whole corpus
  oov_surface_shapes.csv       how the recoverable-OOV raw labels are written
  examples.csv                 up to 25 named GSM examples per class, so any
                               count can be spot-checked against the corpus

Generating scripts in this folder: error_analysis.py, acronym_audit.py

Run (from this folder):
  python3 error_analysis.py \
    ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz \
    ../../../reference/mesh.sqlite ../../../reference/cellosaurus.sqlite \
    .. ../S11_age_analysis/age_composition.py \
    ../../../code/5_repaired_pipeline/phase2 .

Causes are kept separate because they do not have the same remedy:

  extraction     a value was read that the record does not support.
  normalization  a value was read correctly but mapped to no concept, or left
                 out-of-vocabulary when a term for it existed.
  acronym        a short form the study-scoped expansion did not resolve.

What is NOT an error, and is therefore in not_errors.csv:

  removed_by_field_scope
                 a value moved to the field that owns it. A cell line is a
                 Tissue, so taking it out of Condition or Treatment and routing
                 it to Tissue is the pipeline being right; S22 shows the target
                 field already carried the value in 100% of cases, which makes
                 the removal a deduplication rather than a loss.

  not_specified  the record states nothing, so "Not Specified" is the correct
                 answer. A pipeline that invented a value would be the one in
                 error. This bounds coverage, not accuracy.
  life_stage_descriptor
                 the record gives "adult" or "newborn" rather than a number.
                 Reading it as written is correct.
  concept_outside_field_branch
                 the concept fits the label but sits outside the MeSH branch
                 this paper scopes the field to - Gene Knockdown Techniques is
                 in E05, Stress, Psychological in F01. Counted so the sweep is
                 not reported as narrower than it was.

Two classes are deliberately conservative:

  oov_though_vocabulary_term_existed is a deterministic lower bound. It counts
  only labels whose canonical form is an exact in-branch name or synonym, the
  same test S7 and S14 apply. The LLM-judge audit in S7 estimates the true rate
  to be higher; that estimate is reported there with its own caveats and is not
  mixed into these counts.

  oov_surface_shapes.csv is descriptive, not causal. The canonicalization
  already folds case, punctuation and underscores, so an unusual surface is not
  by itself the reason an exact match was not reached. The shapes are given
  because they cluster, which says where to look next; the file does not claim
  they are the cause.

The unresolved acronym residue is split deterministically by acronym_audit.py,
not adjudicated. Matching the acronym string against MeSH cannot decide it: MeSH
indexes expansions, not abbreviations, so BCP-ALL, HNSqCC and CNS-PNET look
unresolvable by that test while their descriptors exist (D015452, D000077195,
D018242). The expansion is therefore taken from each study's own title, summary
and design with the pipeline's own find_definition, and looked up in the shipped
mesh.sqlite. 947 occurrences (0.118% of the corpus) are recoverable on that
test: the study spells the abbreviation out and a Condition-branch descriptor
denotes it. 10,512 are never spelled out in the study that used them and are
left unattributed, because deciding them would need a source outside the study.

value_present_no_concept counts labels read correctly for which no controlled
concept was assigned. Inspecting them (see examples.csv) shows they are largely
experimental descriptors no vocabulary covers - Mock, Naive - so this class is
closer to a vocabulary gap than to a pipeline fault.

Inputs shipped for this analysis:
  ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz
  ../../../reference/{mesh,cellosaurus}.sqlite
  ../S11_age_analysis/age_composition.py   (the Age classifier, reused not restated)
  ../S23_integrity_screens/still_failing.csv
  ../S9_gse_shortform_audit/E_gse_shortform_audit.csv
  ../../../code/5_repaired_pipeline/phase2/acronym_expand.py
