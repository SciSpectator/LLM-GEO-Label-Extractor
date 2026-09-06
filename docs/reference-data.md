# Reference data

Reference databases are not vendored because they are large and independently
versioned. Record the exact release, download date, checksum, and source URL in
every production run.

Required inputs:

- **GEOmetadb SQLite**: source GSM/GSE metadata and the corpus accession set.
- **MeSH XML**: descriptors, entry terms, supplementary concepts, and tree
  numbers used for exact lookup and branch checks.
- **Cellosaurus flat file**: cell-line names, synonyms, and CVCL identifiers.
- **BioLORD-2023 checkpoint**: semantic retrieval embeddings.
- **SapBERT checkpoint**: semantic assistance for OOV consolidation.

The builders create `mesh.sqlite`, `vocab.sqlite`, `cellosaurus.sqlite`, and
`vocab_index.npz`. These generated artifacts are ignored by Git. For archival
reproduction, publish them with checksums in a versioned research-data deposit,
not as moving files in the source repository.
