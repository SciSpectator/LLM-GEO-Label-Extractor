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
    "CELLLINE_DB", str(Path(__file__).resolve().parent / "cellosaurus.sqlite"))

_ALNUM_HYPHEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*[A-Za-z0-9]")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]{3,}")


class CellLineDB:
    """Case-insensitive exact lookup into the Cellosaurus name vocabulary.
    Missing DB => every match() returns None (graceful: caller falls back to
    the LLM cell-line gate), so the pipeline still runs without the reference."""

    _CACHE: dict = {}   # db_path -> CellLineDB

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
        # Per-thread connection. A single shared sqlite3 handle is NOT safe for
        # concurrent use by worker threads (raises InterfaceError "bad parameter
        # or other API misuse"); each thread gets its own read-only connection,
        # mirroring mesh_lookup's thread-local handle.
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
        return self._exact_unambiguous(name) is not None

    # A cell-line match whose OWN Cellosaurus organism is not in the corpus's
    # species is wrong regardless of ambiguity or xenograft host: the matched
    # NAME denotes a different organism's line. For the published corpus that
    # species set is {Homo sapiens}, since the corpus is defined as human GEO
    # samples ("we evaluated all 804,427 human GEO samples").
    #
    # It is a property of the CORPUS, not of the code, so it is configurable:
    # a multi-species corpus (ARCHS4, for instance, carries human and mouse)
    # must not have its legitimate mouse lines rejected as a side effect of a
    # rule written for a human-only run.
    #
    #     CORPUS_SPECIES="Homo sapiens;Mus musculus"   -> both accepted
    #     CORPUS_SPECIES=""                            -> no species filter
    #
    # Unset keeps the published behaviour.
    _SPECIES = {
        s.strip() for s in os.environ.get("CORPUS_SPECIES", "Homo sapiens").split(";")
        if s.strip()
    }

    def _exact_unambiguous(self, name: str) -> tuple[str, str] | None:
        """REPAIR-1/2, revised twice:
        (a) reject when ``name`` is registered under MORE THAN ONE distinct
            CVCL entry, e.g. "R4" (NB4-R4 AND Ras[V12]-H4) or "S1" (5
            unrelated lines, one of them Drosophila). A length floor was
            tried first and rejected: QIMR institute codes are exactly 3
            characters (HW1, JK2, MN1, PB1, RN1, WK1) and a floor of 4
            blocks 6/10 of them even though each is an unambiguous
            single-entry match. Ambiguity is the actual defect, not length.
        (b) reject a UNIQUE match too when its own Cellosaurus organism
            tag excludes human -- this is the majority defect class
            (1,012 samples; 74.2% are unique matches, so ambiguity alone
            fixes only 25.8% of them; the corpus's human-only premise
            fixes the rest by direct rejection, not disambiguation).
        Returns (cvcl, primary_name) iff exactly one distinct CVCL owns
        this name/synonym AND that entry's organism intersects the corpus's
        species set (see _SPECIES); None otherwise.
        """
        if not self._con or not name:
            return None
        rows = self._con.execute(
            "SELECT DISTINCT cvcl, primary_name, organism FROM cell_lines "
            "WHERE name = ? COLLATE NOCASE", (name,)).fetchall()
        distinct_cvcl = {r[0] for r in rows}
        if len(distinct_cvcl) != 1:
            return None
        cvcl, primary_name, organism = rows[0]
        if self._SPECIES and organism:
            orgs = {o.strip() for o in organism.split(";") if o.strip()}
            if not (orgs & self._SPECIES):
                return None
        return (cvcl, primary_name)

    def _embedded(self, value: str) -> str | None:
        cands = set(_ALNUM_HYPHEN.findall(value)) | set(_ALNUM_RUN.findall(value))
        hits = [t for t in cands
                if len(t) >= 3 and re.search(r"[A-Za-z]", t) and re.search(r"\d", t)
                and self._exact_unambiguous(t) is not None]
        return max(hits, key=len) if hits else None

    def match(self, value: str) -> str | None:
        if not self._con:
            return None
        v = (value or "").strip()
        if not v:
            return None
        if self._exact(v):
            return v                      # whole value is a recognised line
        tok = self._embedded(v)
        if tok:
            # compact identifier (no whitespace) -> keep the full construct
            # (derived / clone name); a phrase -> return the recognised line.
            return v if " " not in v else tok
        return None

    def _accession(self, name: str) -> tuple[str, str] | None:
        """(cvcl, primary_name) for a name, or None if it does not resolve.

        The released form of this method ended in ``LIMIT 1``, which is the
        same silent-first-wins defect as elsewhere in the vocabulary path: when
        several lines share a name, one was returned as though it were the only
        candidate. Resolution here goes through the same unambiguity+organism
        gate as every other lookup, so a name cannot resolve by one route what
        it is refused by another.
        """
        return self._exact_unambiguous(name)

    def match_ref(self, value: str):
        """Like match(), but resolve to canonical (CVCL accession, primary_name)
        so every spelling of one line folds to a single id (MCF7 / MCF-7 /
        'MCF7 cells' -> CVCL_0031, 'MCF-7'). Returns (cvcl, primary_name), or
        (None, ref) when the matched token is a derived/compact construct that
        is not itself a catalogued name, or None when the value names no
        established line."""
        ref = self.match(value)
        if not ref:
            return None
        row = self._accession(ref)
        if row:
            return row
        tok = self._embedded(value)
        if tok and tok != ref:
            row = self._accession(tok)
            if row:
                return row
        return (None, ref)

    def donor_sex(self, cvcl: str) -> str | None:
        """Attested donor sex for an accession, or None when the catalogue does
        not constrain it ('Sex unspecified', 'Sex ambiguous', 'Mixed sex', or a
        database built before this column existed).

        A catalogued line's donor sex is a property of the IDENTITY, so for a
        sample that IS this line it outranks a Sex inferred from surrounding
        study text. Exposed here so the Sex path can consult it instead of the
        contradiction only being detectable offline, after release.
        """
        if not self._con or not cvcl:
            return None
        try:
            row = self._con.execute(
                "SELECT DISTINCT donor_sex FROM cell_lines WHERE cvcl = ?",
                (cvcl,)).fetchone()
        except sqlite3.OperationalError:
            return None               # DB predates the donor_sex column
        if not row or not row[0]:
            return None
        return row[0] if row[0] in ("Male", "Female") else None
