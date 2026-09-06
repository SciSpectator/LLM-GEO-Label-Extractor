Table S9. GSE-context short-form expansion audit. Each per-study acronym
expansion is checked against that study's own title/summary/design; 'verified'
means the expanded concept is grounded in the GSE text, 'context-present' that
the acronym is attested there and the expansion is its standard meaning.

Result file in this folder: E_gse_shortform_audit.csv (one row per expansion event)
Generating script in this folder: phase2_gse_shortform_eval.py

The script has no CLI: it reads four path constants at the top of the file.
Substitute them exactly as written, then run `python3 phase2_gse_shortform_eval.py`.

  DICT = "Directory to Phase 2 dictionary.json.gz"
      -> ../../06_final_labels_repaired/phase2_out/dictionary.json.gz
  GSEM = "Directory to gse_meta_scraped.json.gz"
      -> ../../01_input_metadata/gse_meta_scraped.json.gz
  MESH = "Directory to reference mesh.sqlite"
      -> ../../../reference/mesh.sqlite
  OUT  = "Directory to output"
      -> any writable directory

The constant names differ from the file names they take (GSEM is the scraped
GSE metadata, not the per-sample geo_metadata.sqlite), so match them by the
placeholder text above rather than by guessing from the variable name.

What the run prints, and what the table reports:
  1,453 GSE-context short-form expansions audited
  866 verified (59.6%), +120 context-present, 986 attested either way (67.9%)
  Tissue 192/279, Condition 104/172, Treatment 570/1,002 verified
  instance-weighted attested 14,190/20,780 (68.3%)

Inputs shipped for this table:
  ../../06_final_labels_repaired/phase2_out/dictionary.json.gz
  ../../01_input_metadata/gse_meta_scraped.json.gz
  ../../../reference/mesh.sqlite

geo_metadata.sqlite is not shipped; build it first from the metadata that is:
    python3 ../../01_input_metadata/build_geo_metadata.py
