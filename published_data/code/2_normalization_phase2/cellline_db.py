#!/usr/bin/env python3
"""Deterministic cell-line recognition against the Cellosaurus reference
vocabulary (cellosaurus.sqlite, built by build_cellosaurus_db.py).

A REFERENCE vocabulary — like mesh.sqlite for diseases/tissues/drugs — NOT a
hardcoded example list: recognized names come from Cellosaurus (~169k lines,
~298k names incl. synonyms), never from any scanned GEO sample. This lets
Phase 2 keep an established cell line's identifier verbatim even when the LLM
gate does not RECOGNISE the (obscure / prefixed) line by world knowledge, and
correctly handles a sample that names MULTIPLE cell lines (each ';'-component
is matched independently by the caller).

``match(value)`` returns the bare cell-line identity to KEEP (bypassing MeSH
normalisation), or None when the value names no established line:
  - exact full-value match (case-insensitive)        -> keep the value as-is
  - a compact identifier that EMBEDS a line token     -> keep the full construct
    (e.g. 'mPDE10-HEK293' -> kept; the transfection prefix is not a tissue)
  - a phrase that mentions a line token               -> return the line token
Embedded tokens must mix letters+digits and be >=3 chars (typical line-code
shape) so ordinary tissue words never false-match.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path

DB_PATH = os.environ.get(
    "CELLLINE_DB", str(Path(__file__).resolve().parent / "cellosaurus.sqlite")
)

_ALNUM_HYPHEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*[A-Za-z0-9]")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]{3,}")


class CellLineDB:
    """Case-insensitive exact lookup into the Cellosaurus name vocabulary.
    Missing DB => every match() returns None (graceful: caller falls back to
    the LLM cell-line gate), so the pipeline still runs without the reference."""

    _CACHE: dict = {}

    @classmethod
    def get(cls, db_path: str = DB_PATH) -> "CellLineDB":
        inst = cls._CACHE.get(db_path)
        if inst is None:
            inst = cls(db_path)
            cls._CACHE[db_path] = inst
        return inst

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._exists = Path(db_path).exists()
        self._local = threading.local()

    @property
    def _con(self) -> "sqlite3.Connection | None":

        if not self._exists:
            return None
        c = getattr(self._local, "con", None)
        if c is None:
            c = sqlite3.connect(self.db_path, check_same_thread=False)
            c.execute("PRAGMA query_only=ON")
            self._local.con = c
        return c

    def _exact(self, name: str) -> bool:
        if not self._con or not name:
            return False
        row = self._con.execute(
            "SELECT 1 FROM cell_lines WHERE name = ? COLLATE NOCASE LIMIT 1", (name,)
        ).fetchone()
        return row is not None

    def _embedded(self, value: str) -> str | None:
        cands = set(_ALNUM_HYPHEN.findall(value)) | set(_ALNUM_RUN.findall(value))
        hits = [
            t
            for t in cands
            if len(t) >= 3
            and re.search(r"[A-Za-z]", t)
            and re.search(r"\d", t)
            and self._exact(t)
        ]
        return max(hits, key=len) if hits else None

    def match(self, value: str) -> str | None:
        if not self._con:
            return None
        v = (value or "").strip()
        if not v:
            return None
        if self._exact(v):
            return v
        tok = self._embedded(v)
        if tok:

            return v if " " not in v else tok
        return None

    def _accession(self, name: str):
        """(cvcl, primary_name) for a Cellosaurus name, or None."""
        if not self._con or not name:
            return None
        return self._con.execute(
            "SELECT cvcl, primary_name FROM cell_lines WHERE name = ? "
            "COLLATE NOCASE LIMIT 1",
            (name,),
        ).fetchone()

    def match_ref(self, value: str):
        """Like match(), but resolve the recognised line to its canonical
        Cellosaurus (CVCL accession, primary_name) so every spelling of one
        line folds to a single id (MCF7 / MCF-7 / 'MCF7 cells' -> CVCL_0031,
        'MCF-7'). Returns (cvcl, primary_name), or (None, ref) when the matched
        token is a derived/compact construct that is not itself a catalogued
        name, or None when the value names no established line."""
        ref = self.match(value)
        if not ref:
            return None
        row = self._accession(ref)
        if row:
            return (row[0], row[1])

        tok = self._embedded(value)
        if tok and tok != ref:
            row = self._accession(tok)
            if row:
                return (row[0], row[1])
        return (None, ref)
