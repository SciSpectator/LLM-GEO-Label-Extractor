"""Parse NLM MeSH descriptor XML into a SQLite DB.

Input  : $MESH_XML (default ./desc2026.xml — NLM 2026 DescriptorRecordSet)
Output : $MESH_DB  (default ./mesh.sqlite, alongside this script)

Schema:
  mesh_terms     (id TEXT PK, name TEXT, scope TEXT, category CHAR)
                 category = first char of any tree number (A/B/C/D/E/F/...)
                           or '' if descriptor has no tree number.
  mesh_synonyms  (mesh_id TEXT, synonym TEXT)            -- all Concept/Term Strings
  mesh_tree      (tree_number TEXT, mesh_id TEXT)        -- tree number -> descriptor
  mesh_parent    (parent_id TEXT, child_id TEXT)         -- derived from tree prefixes
  oov_mesh_clusters (id TEXT PK, label TEXT, col TEXT, source TEXT,
                     created_at TEXT, occurrences INTEGER)
                 -- the out-of-distribution (OOV) mesh: starts EMPTY;
                 -- runtime mints rows with source='minted' when a Phase 2
                 -- label has no MeSH match.
  oov_mesh_synonyms (oov_mesh_id TEXT, synonym TEXT)

Indexes:
  mesh_terms.name, mesh_synonyms.synonym (collation NOCASE for case-insensitive lookup),
  mesh_synonyms.mesh_id, mesh_tree.mesh_id, mesh_parent.parent_id, mesh_parent.child_id.

Tier categories of interest for the LLM extractor:
  A  -> Anatomy            (Tissue)
  C  -> Diseases           (Condition)
  F03 -> Mental disorders  (Condition)
  D  -> Chemicals & drugs  (Treatment)
  E  -> Analytical/Diagnostic/Therapeutic Techniques (Treatment)
"""

import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

_HERE = Path(__file__).resolve().parent
XML_IN = os.environ.get("MESH_XML", str(_HERE / "desc2026.xml"))
DB_OUT = os.environ.get("MESH_DB", str(_HERE / "mesh.sqlite"))

DDL = """
DROP TABLE IF EXISTS mesh_terms;
DROP TABLE IF EXISTS mesh_synonyms;
DROP TABLE IF EXISTS mesh_tree;
DROP TABLE IF EXISTS mesh_parent;
DROP TABLE IF EXISTS oov_mesh_clusters;
DROP TABLE IF EXISTS oov_mesh_synonyms;

CREATE TABLE mesh_terms (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL COLLATE NOCASE,
    scope     TEXT,
    category  TEXT
);
CREATE TABLE mesh_synonyms (
    mesh_id   TEXT NOT NULL,
    synonym   TEXT NOT NULL COLLATE NOCASE
);
CREATE TABLE mesh_tree (
    tree_number TEXT NOT NULL,
    mesh_id     TEXT NOT NULL
);
CREATE TABLE mesh_parent (
    parent_id TEXT NOT NULL,
    child_id  TEXT NOT NULL
);
CREATE TABLE oov_mesh_clusters (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL COLLATE NOCASE,
    col         TEXT NOT NULL,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE oov_mesh_synonyms (
    oov_mesh_id  TEXT NOT NULL,
    synonym      TEXT NOT NULL COLLATE NOCASE
);
"""

INDEXES = """
CREATE INDEX idx_terms_name      ON mesh_terms(name);
CREATE INDEX idx_syn_synonym     ON mesh_synonyms(synonym);
CREATE INDEX idx_syn_meshid      ON mesh_synonyms(mesh_id);
CREATE INDEX idx_tree_meshid     ON mesh_tree(mesh_id);
CREATE INDEX idx_parent_pid      ON mesh_parent(parent_id);
CREATE INDEX idx_parent_cid      ON mesh_parent(child_id);
CREATE INDEX idx_terms_category  ON mesh_terms(category);
CREATE INDEX idx_oov_mesh_label     ON oov_mesh_clusters(label);
CREATE INDEX idx_oov_mesh_col       ON oov_mesh_clusters(col);
CREATE INDEX idx_oov_mesh_source    ON oov_mesh_clusters(source);
CREATE INDEX idx_oov_mesh_syn       ON oov_mesh_synonyms(synonym);
CREATE INDEX idx_oov_mesh_syn_owner ON oov_mesh_synonyms(oov_mesh_id);
"""


def parse_record(rec: ET.Element):
    """Yield (mesh_terms-row, [synonyms], [tree_numbers]) for one DescriptorRecord."""
    ui = rec.findtext("DescriptorUI") or ""
    name = rec.findtext("DescriptorName/String") or ""
    if not ui:
        return None

    scope = ""
    synonyms: set[str] = set()
    for concept in rec.findall("ConceptList/Concept"):
        if concept.get("PreferredConceptYN") == "Y":
            scope = (concept.findtext("ScopeNote") or "").strip()
        for term in concept.findall("TermList/Term"):
            s = (term.findtext("String") or "").strip()
            if s:
                synonyms.add(s)
    synonyms.discard(name)

    tree_numbers = [t.text for t in rec.findall("TreeNumberList/TreeNumber") if t.text]
    category = tree_numbers[0][0] if tree_numbers else ""

    return (ui, name, scope, category), sorted(synonyms), tree_numbers


def main():
    if not Path(XML_IN).exists():
        sys.exit(f"missing {XML_IN}")
    Path(DB_OUT).unlink(missing_ok=True)

    con = sqlite3.connect(DB_OUT)
    con.executescript(DDL)

    t0 = time.time()
    rows_terms: list[tuple] = []
    rows_syn: list[tuple] = []
    rows_tree: list[tuple] = []
    n = 0

    for event, elem in ET.iterparse(XML_IN, events=("end",)):
        if elem.tag != "DescriptorRecord":
            continue
        parsed = parse_record(elem)
        if parsed:
            term, syns, trees = parsed
            rows_terms.append(term)
            rows_syn.extend((term[0], s) for s in syns)
            rows_tree.extend((tn, term[0]) for tn in trees)
            n += 1
            if n % 5000 == 0:
                print(f"  parsed {n :>6d}  ({time.time()-t0 :5.1f}s)")
        elem.clear()

    print(f"\nDescriptors parsed: {n}  ({time.time()-t0 :.1f}s)")

    con.executemany("INSERT INTO mesh_terms VALUES (?,?,?,?)", rows_terms)
    con.executemany("INSERT INTO mesh_synonyms VALUES (?,?)", rows_syn)
    rows_tree = sorted(set(rows_tree))
    con.executemany("INSERT INTO mesh_tree VALUES (?,?)", rows_tree)
    con.commit()

    print("Deriving parent-child edges from tree numbers...")
    cur = con.cursor()
    cur.execute("SELECT tree_number, mesh_id FROM mesh_tree")
    tn_to_id: dict[str, list[str]] = {}
    for tn, mid in cur.fetchall():
        tn_to_id.setdefault(tn, []).append(mid)
    edges: list[tuple] = []
    for tn, child_ids in tn_to_id.items():
        if "." not in tn:
            continue
        parent_tn = tn.rsplit(".", 1)[0]
        parent_ids = tn_to_id.get(parent_tn, [])
        for child_id in child_ids:
            for parent_id in parent_ids:
                if parent_id != child_id:
                    edges.append((parent_id, child_id))

    edges = sorted(set(edges))
    cur.executemany("INSERT INTO mesh_parent VALUES (?,?)", edges)
    con.commit()
    print(f"  edges: {len(edges)}")

    print("Building indexes...")
    con.executescript(INDEXES)

    con.executescript("""
        CREATE TABLE IF NOT EXISTS resolutions (
            input_label TEXT NOT NULL COLLATE NOCASE, col TEXT NOT NULL,
            output_id TEXT NOT NULL, output_name TEXT NOT NULL,
            source TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_res_input ON resolutions(input_label);
        CREATE INDEX IF NOT EXISTS idx_res_col ON resolutions(col);
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_res_label_col
            ON resolutions(input_label COLLATE NOCASE, col);
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_oov_mesh_col_label
            ON oov_mesh_clusters(col, label COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS verifier_decisions (
            raw_lc TEXT NOT NULL, col TEXT NOT NULL, picked_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL, verdict TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (raw_lc, col, picked_id, prompt_version));
        CREATE TABLE IF NOT EXISTS polarity_decisions (
            raw_lc TEXT NOT NULL, col TEXT NOT NULL, prompt_version TEXT NOT NULL,
            polarity TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (raw_lc, col, prompt_version));
    """)
    con.execute("ANALYZE")
    con.commit()
    con.close()

    sz = Path(DB_OUT).stat().st_size / 1024 / 1024
    print(f"\nWrote {DB_OUT}  ({sz :.1f} MB, {time.time()-t0 :.1f}s total)")


if __name__ == "__main__":
    main()
