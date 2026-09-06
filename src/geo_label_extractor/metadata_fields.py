"""Which metadata columns the extractor puts in front of the model.

A platform can be chosen freely, so the columns should be choosable too: GEO
records differ between an RNA-seq submission and an array one, and a column that
carries the interesting text on one platform is empty on another.

The default is deliberately frozen. It is the five-column set the published run
used, and reproducing the paper's numbers depends on it not moving. Anything
beyond that is opt-in, and the run manifest records what was chosen so a result
can always be traced back to the input it saw.

Column names are GEOmetadb's. Each maps to a prompt field name, because the
prompt artifacts were optimized against those names and renaming them would
silently change what the model reads.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Dict, List, NamedTuple, Optional, Sequence


class Field(NamedTuple):
    column: str          # name in the GEOmetadb `gsm` table
    prompt: str          # name the model sees
    description: str     # shown to the model in the system prompt
    aliases: tuple       # keys a materialized record may use, in priority order
    squash_tabs: bool = True   # see note below

# Alias order and `squash_tabs` are not cosmetic. The published run read
# `gsm_title` and `source_name` specifically, and stripped tabs from only three
# of the five fields. Both are reproduced exactly so that the default selection
# renders a prompt identical to the one the paper's numbers came from.


#: The published five. Changing this changes every downstream number.
DEFAULT_COLUMNS: tuple = (
    "title",
    "source_name_ch1",
    "characteristics_ch1",
    "treatment_protocol_ch1",
    "description",
)

_KNOWN: Dict[str, Field] = {
    # Exactly one alias each: the published run resolved these keys and no
    # others, and a fallback would change what the model reads on records that
    # happen to carry both spellings.
    "title": Field(
        "title", "title", "Sample title.",
        ("gsm_title",), squash_tabs=False),
    "source_name_ch1": Field(
        "source_name_ch1", "source", "Sample source_name field.",
        ("source_name",), squash_tabs=False),
    "characteristics_ch1": Field(
        "characteristics_ch1", "characteristics",
        "Sample characteristics field — usually key:value pairs "
        "separated by ';' or '|'.",
        ("characteristics",)),
    "treatment_protocol_ch1": Field(
        "treatment_protocol_ch1", "treatment_protocol",
        "Sample treatment/lab protocol text.",
        ("treatment_protocol",)),
    "description": Field(
        "description", "description", "Sample description.",
        ("description",)),
    # Columns the default set leaves out. Present in GEOmetadb.
    "extract_protocol_ch1": Field(
        "extract_protocol_ch1", "extract_protocol",
        "Nucleic-acid extraction protocol.", ("extract_protocol_ch1",)),
    "growth_protocol_ch1": Field(
        "growth_protocol_ch1", "growth_protocol",
        "Cell growth or culture protocol.", ("growth_protocol_ch1",)),
    "data_processing": Field(
        "data_processing", "data_processing",
        "Analysis pipeline description.", ("data_processing",)),
    "label_ch1": Field(
        "label_ch1", "label", "Labelling reagent.", ("label_ch1",)),
    "hyb_protocol": Field(
        "hyb_protocol", "hyb_protocol", "Hybridization protocol.",
        ("hyb_protocol",)),
    "molecule_ch1": Field(
        "molecule_ch1", "molecule", "Molecule assayed, e.g. total RNA.",
        ("molecule_ch1",)),
    "organism_ch1": Field(
        "organism_ch1", "organism", "Source organism.", ("organism_ch1",)),
    "channel_count": Field(
        "channel_count", "channel_count", "Number of channels.",
        ("channel_count",)),
    "type": Field("type", "sample_type", "GEO sample type.", ("type",)),
    "supplementary_file": Field(
        "supplementary_file", "supplementary_file",
        "Supplementary file names.", ("supplementary_file",)),
    # ARCHS4-only columns, usable when the input carries them.
    "relation": Field(
        "relation", "relation",
        "Links to related records, e.g. BioSample, SRA, or a reanalysed series.",
        ("relation",)),
    "taxid_ch1": Field(
        "taxid_ch1", "taxid", "NCBI taxonomy id.", ("taxid_ch1",)),
    "library_selection": Field(
        "library_selection", "library_selection",
        "Library selection method.", ("library_selection",)),
    "library_source": Field(
        "library_source", "library_source", "Library source.",
        ("library_source",)),
    "library_strategy": Field(
        "library_strategy", "library_strategy",
        "Sequencing strategy, e.g. RNA-Seq.", ("library_strategy",)),
    "instrument_model": Field(
        "instrument_model", "instrument_model", "Sequencing instrument.",
        ("instrument_model",)),
    "singlecellprobability": Field(
        "singlecellprobability", "singlecell_probability",
        "ARCHS4 estimate that the sample is single-cell.",
        ("singlecellprobability",)),
}

#: Environment variable the pipeline sets so worker stages see the same choice.
ENV_VAR = "GEO_EXTRACT_FIELDS"


def describe(column: str) -> Field:
    """The Field for a column, inventing a reasonable one if unknown."""
    if column in _KNOWN:
        return _KNOWN[column]
    prompt = column[:-4] if column.endswith("_ch1") else column
    return Field(column, prompt, f"GEO `{column}` field.", (column, prompt))


def available(db_path: str) -> List[str]:
    """Text-bearing columns the given GEOmetadb actually has."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return list(DEFAULT_COLUMNS)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(gsm)")]
    except sqlite3.Error:
        cols = []
    finally:
        con.close()
    skip = {"ID", "gsm", "series_id", "gpl", "status",
            "submission_date", "last_update_date", "contact"}
    return [c for c in cols if c not in skip] or list(DEFAULT_COLUMNS)


def parse(selection: Optional[str]) -> List[str]:
    """Turn a --fields value into a column list. Empty means the default."""
    if not selection or not selection.strip():
        return list(DEFAULT_COLUMNS)
    seen, out = set(), []
    for part in selection.replace(";", ",").split(","):
        col = part.strip()
        if col and col not in seen:
            seen.add(col)
            out.append(col)
    return out or list(DEFAULT_COLUMNS)


def active() -> List[Field]:
    """The selection in force for this process."""
    return [describe(c) for c in parse(os.environ.get(ENV_VAR))]


def publish(columns: Sequence[str]) -> None:
    """Make a selection visible to every stage started from here."""
    os.environ[ENV_VAR] = ",".join(columns)


def is_default(columns: Sequence[str]) -> bool:
    return tuple(columns) == DEFAULT_COLUMNS


def values_from(raw: Dict) -> Dict[str, str]:
    """Prompt field values for one materialized sample record.

    Records reach this point under two naming conventions depending on the
    stage that wrote them, so every alias is tried before giving up.
    """
    out: Dict[str, str] = {}
    for f in active():
        value = ""
        for key in f.aliases:
            candidate = raw.get(key)
            if candidate not in (None, ""):
                value = str(candidate)
                break
        if f.squash_tabs:
            value = value.replace("\t", " ")
        out[f.prompt] = value.strip()
    return out
