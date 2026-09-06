Main Table 3. Normalization quality: collapse.csv = raw->canonical fragmentation collapse (raw values, canonical concepts, ratio); branch_validity.csv = MeSH branch-validity violations; consistency.csv = identical-input consistency (disagreement rate over identical raw labels). These three CSVs also supply the canonical-concept and MeSH-descriptor counts summarised in Supplementary Table S2.

Result file(s) in this folder: collapse.csv, branch_validity.csv, consistency.csv
Generating script in this folder: phase2_evaluation.py
Run:
  python phase2_evaluation.py <phase2_output_dir> <mesh.sqlite> <cellosaurus.sqlite> <out_dir>
  (produces db_resolution, acronym_expansion, vocabulary_precision, composition, collapse,
   branch_validity, consistency; the file(s) for THIS table are listed above)
