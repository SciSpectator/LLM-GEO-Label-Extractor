# Tutorial

## 1. Prepare Python

```bash
# Debian/Ubuntu desktop dependencies for the optional Qt GUI
sudo apt-get install libegl1 libgl1 libxkbcommon-x11-0

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## 2. Prepare GEO and reference vocabularies

Place `GEOmetadb.sqlite` under `data/`. Download the desired MeSH XML release
and Cellosaurus flat file, then build the local read-only databases:

```bash
python src/geo_label_extractor/build_mesh_db.py desc.xml data/reference/mesh.sqlite
MESH_DB=data/reference/mesh.sqlite \
  python src/geo_label_extractor/build_vocab.py
CELLOSAURUS_TXT=cellosaurus.txt \
CELLLINE_DB=data/reference/cellosaurus.sqlite \
  python src/geo_label_extractor/build_cellosaurus_db.py
python src/geo_label_extractor/build_index.py \
  data/reference/vocab.sqlite \
  data/reference/cellosaurus.sqlite \
  data/reference/vocab_index.npz
```

Check the builders' `--help` or module docstrings because upstream vocabulary
formats can change between releases.

## 3. Start inference

Serve the extraction and normalization models through one or more
OpenAI-compatible endpoints. The served names must equal the identifiers passed
to the CLI. Example environment:

```bash
cp .env.example .env
set -a; source .env; set +a
```

If replicas are available, provide comma-separated Phase-2 endpoints in
`PHASE2_VLLM_URLS`. Do not commit `.env`.

## 4. Development run

```bash
geo-label-extractor \
  --input data/GEOmetadb.sqlite \
  --out-dir results/demo \
  --vocab data/reference/vocab.sqlite \
  --index data/reference/vocab_index.npz \
  --cellosaurus data/reference/cellosaurus.sqlite \
  --limit 100 --extract-workers 4 --normalize-workers 8
```

This runs all three stages end to end: extraction, normalization, and final
assembly. Inspect the extraction shards, completion markers, normalized
dictionary, per-platform exports, and audit logs under `results/demo`, and the
merged corpus under `results/demo/final_labels/`
(`LLM_labels_all_samples.csv.gz`, `by_GPL/`, `manifest.json`).

Selection can be provided at any scale:

```bash
# Exact samples
geo-label-extractor ... --gsm-manifest my_gsms.txt

# Entire platforms
geo-label-extractor ... --gpl GPL570 --gpl GPL96

# LLM-assisted GEO catalogue search and selected fields
geo-label-extractor ... \
  --spec "Homo sapiens breast cancer, Tissue and Condition, Phase 1b only"

# Explicit phase and fields always override the inferred specification
geo-label-extractor ... --gsm GSM123 --labels Tissue,Condition \
  --stop-after phase1
```

Use `geo-label-gui` for the same controls without constructing a command.

## 5. Resume or rerun

```bash
# Reuse extraction and restart at normalization
geo-label-extractor ... --from-stage normalize

# Ignore a normalization completion marker
geo-label-extractor ... --from-stage normalize --force

# Only rebuild the merged corpus from existing Phase 2 exports
geo-label-extractor ... --from-stage assemble --force

# Stop after normalization, before the final merge
geo-label-extractor ... --no-assemble
```

Never combine independently normalized dictionaries without checking for raw
value overlap. The standard run deliberately builds one global dictionary.
