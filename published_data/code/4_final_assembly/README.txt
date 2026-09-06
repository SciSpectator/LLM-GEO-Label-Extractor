Final assembly — see ../../data/merge_final_labels.py

The Phase 2 per-platform outputs contain only normalized Tissue/Condition/Treatment.
merge_final_labels.py adds final_Sex (from Phase 1/1b) and final_Age (from the Age run)
to every sample and writes LLM_labels_all_samples.csv.gz (all five labels).
Set the four directory placeholders at the top of that script before running.
