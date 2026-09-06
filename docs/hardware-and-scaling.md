# Hardware and scaling

Actual memory depends on the exact checkpoint, quantization format, inference
engine, context occupancy, batch size, and number of concurrent requests. The
figures below are practical planning ranges, not guaranteed peaks.

## Model-serving VRAM

| Workload | Weight footprint | Practical minimum VRAM | Recommended VRAM |
|---|---:|---:|---:|
| Phase 1/1b, 12B model with FP8 weights and 8k context | about 12 GB plus KV/runtime | 16 GB at low concurrency | 24 GB |
| Age or Phase 2, effective-2B model in BF16 | about 4 GB plus KV/runtime | 8 GB | 12-16 GB |
| BioLORD/SapBERT embedding and retrieval only | generally below 2 GB | 4 GB or CPU mode | 8 GB |
| Sequential full pipeline on one device | models loaded one at a time | 24 GB | 32 GB |
| Concurrent 12B and e2B services on one device | both models resident | 32 GB with conservative batching | 48 GB |

If memory is limited, run Phase 1/1b first, stop that model server, start the
Phase-2 model, and resume with `--from-stage normalize`. Reduce workers before
reducing context length. Truncating metadata changes the evidence presented to
the extractor.

## Host memory and disk

| Scale | RAM | Free disk | Suggested workers |
|---|---:|---:|---:|
| Development, up to 1,000 GSM | 16 GB | 40 GB | 2-8 extraction / 4-16 normalization |
| One or several GPLs, up to 100k GSM | 32-64 GB | 60 GB | 8-32 / 32-128 |
| Publication-scale, about 804k GSM | 128 GB recommended | 100 GB recommended | 32-64 / 128-512 across endpoints |

The GEOmetadb snapshot is roughly tens of gigabytes. Model weights, vocabulary
indexes, checkpoints, dictionaries, and exports require additional space. Keep
at least 20% free disk for atomic checkpoints and temporary exports.

## Scaling rules

- Selection by `--gsm-manifest` and `--gpl-manifest` uses temporary SQLite
  tables, avoiding command-line length and SQLite parameter limits.
- Phase 1 is sample-parallel. Increase `--extract-workers` only while endpoint
  latency and VRAM remain stable.
- Phase 1b is GSE-aware. Samples retain study grouping even when GSMs from many
  platforms are selected.
- Phase 2 resolves the global unique-label dictionary. Do not independently
  normalize overlapping shards, because that can assign one raw string twice.
- `PHASE2_VLLM_URLS` accepts comma-separated OpenAI-compatible replicas. Increase
  `--normalize-workers` with the number and capacity of replicas.
- Checkpoints allow a large run to resume after interruption. Use separate
  output directories for separate configurations.

Monitor the inference server's resident VRAM and request queue during a short
representative run before launching an entire platform or corpus.
