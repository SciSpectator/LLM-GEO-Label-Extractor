# Reproducibility and limitations

For a reproducible release, archive:

1. the GEO accession manifest and snapshot date
2. MeSH and Cellosaurus versions and checksums
3. exact model repositories and immutable revisions
4. inference-engine version and decoding parameters
5. prompts and source commit
6. extraction checkpoints, normalization dictionary, and audit logs
7. private human-adjudication records and the evaluation protocol, where data
   governance permits their archival

The model identifiers in `.env.example` describe the manuscript assignment but
must be replaced by the exact served identifiers/revisions available to the
operator. A floating model tag is insufficient for archival reproducibility.

Limitations. GEO metadata may be incomplete or contradictory. GSE context can
support recovery but cannot create sample-specific evidence. Controlled
vocabularies lag emerging concepts, and OOV clustering is not equivalent to
expert ontology curation. Deterministic decoding does not guarantee
bit-identical outputs across inference stacks. Whole-corpus structural audits
are not semantic accuracy estimates.
