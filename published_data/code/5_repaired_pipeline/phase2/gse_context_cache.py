"""Per-(GSE, GSM) context cache shared across Phase 1 / 1b / 1c / 2.

Goal
----
Phase 1 / 1b each build the same GSE+GSM "context" object every time they
run. Phase 1c then *re-derives* sibling features from scratch (BioLORD
embeddings, char-n-grams, JS-divergence). All of this is recomputable
from the GSM raw metadata + the per-GSE distribution of Phase 1 results.

This module persists:

  * the normalised raw-text blob per (GSE, GSM)
  * the structural key set of ``characteristics_ch1``  (sibling-twin test)
  * the per-phase label assignments per (GSE, GSM, field)
  * a per-(GSE, field) aggregate computed once, refreshed on write

Phase 1c can then run a *pure-context* sibling-consensus pass with no
embeddings and no fresh tokenisation — just SQL reads.

Schema
------
    gsm_context (gse, gsm) PK
        title, source_name, characteristics, treatment_protocol,
        description, char_keys (JSON sorted list), updated_at
    gsm_phase_results (gse, gsm, field) PK
        p1_value, p1b_value, p1c_value, p2_value, updated_at
    gse_field_aggregate (gse, field) PK
        n_total, n_ns, value_dist (JSON), struct_keys (JSON), updated_at

The cache is fail-open: if the sqlite file is unwritable, ``GSEContextCache``
falls back to an in-memory dict so callers never crash. There is no
schema migration logic — drop the file to reset.

Universal, no entity hard-coding: nothing in this module references
specific Tissue/Condition/Treatment values.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

LABEL_COLS = ("Tissue", "Condition", "Treatment")


EXTRA_LABEL_COLS = ("Sex", "Age")
ALL_LABEL_COLS = LABEL_COLS + EXTRA_LABEL_COLS
NS = "Not Specified"

_DEFAULT_PATH = os.environ.get(
    "GSE_CONTEXT_CACHE",
    str(Path(__file__).resolve().parent / "gse_context_cache.sqlite"),
)

_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS gsm_context (
    gse                TEXT NOT NULL,
    gsm                TEXT NOT NULL,
    title              TEXT,
    source_name        TEXT,
    characteristics    TEXT,
    treatment_protocol TEXT,
    description        TEXT,
    char_keys          TEXT,           -- JSON list of sorted unique keys
    updated_at         REAL NOT NULL,
    PRIMARY KEY (gse, gsm)
);
CREATE INDEX IF NOT EXISTS idx_ctx_gse ON gsm_context(gse);

CREATE TABLE IF NOT EXISTS gsm_phase_results (
    gse        TEXT NOT NULL,
    gsm        TEXT NOT NULL,
    field      TEXT NOT NULL,
    p1_value   TEXT,
    p1b_value  TEXT,
    p1c_value  TEXT,
    p2_value   TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (gse, gsm, field)
);
CREATE INDEX IF NOT EXISTS idx_phase_gse_field
    ON gsm_phase_results(gse, field);

CREATE TABLE IF NOT EXISTS gse_field_aggregate (
    gse         TEXT NOT NULL,
    field       TEXT NOT NULL,
    n_total     INTEGER NOT NULL,
    n_ns        INTEGER NOT NULL,
    value_dist  TEXT NOT NULL,         -- JSON: {value: count}
    struct_keys TEXT,                  -- JSON: list of sibling key sets
    updated_at  REAL NOT NULL,
    PRIMARY KEY (gse, field)
);

-- Episodic memory for Phase 1 (raw → value).
-- Keyed by content hash so re-runs with the same input skip the LLM.
CREATE TABLE IF NOT EXISTS phase1_episodic (
    raw_hash       TEXT NOT NULL,      -- sha256(title|source|chars|treat|desc)
    field          TEXT NOT NULL,
    value          TEXT NOT NULL,
    model_version  TEXT NOT NULL,      -- e.g. 'gemma4:e2b'
    prompt_version TEXT NOT NULL,      -- compiled prompt artifact id
    updated_at     REAL NOT NULL,
    PRIMARY KEY (raw_hash, field, model_version, prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_p1ep_field ON phase1_episodic(field);

-- Episodic memory for Phase 1b (gsm + gse-context hash → recovered).
-- Keyed by (gsm, hash(GSE meta + sibling Counter), field) so the same
-- (sample, GSE-state) re-runs are O(1).
CREATE TABLE IF NOT EXISTS phase1b_episodic (
    gsm            TEXT NOT NULL,
    gse_hash       TEXT NOT NULL,      -- sha256(gse_meta + sibling_counter)
    field          TEXT NOT NULL,
    p1_value       TEXT,               -- input from Phase 1
    p1b_value      TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (gsm, gse_hash, field, model_version, prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_p1bep_field ON phase1b_episodic(field);

-- Procedural / working: compressed GSE summary (heuristic, no LLM).
-- Stored once per GSE, refreshed on full_hash change. Used by Phase 1b
-- to keep the per-GSE system prompt short → high KV-cache hit rate.
CREATE TABLE IF NOT EXISTS gse_summary_compressed (
    gse                 TEXT PRIMARY KEY,
    full_hash           TEXT NOT NULL,
    summary_compressed  TEXT NOT NULL,
    n_chars_in          INTEGER NOT NULL,
    n_chars_out         INTEGER NOT NULL,
    generated_at        REAL NOT NULL
);

-- Phase 2 per-GSE canonical mapping. Within a single experiment, every
-- semantically equivalent raw label MUST resolve to the same canonical
-- id. This table is consulted BEFORE the global episodic / MeSH lookup
-- tiers so siblings of an already-resolved raw take the cluster
-- decision in O(1) without re-running the LLM picker / verifier.
CREATE TABLE IF NOT EXISTS gse_phase2_canon (
    gse        TEXT NOT NULL,
    col        TEXT NOT NULL,         -- 'Tissue' | 'Condition' | 'Treatment'
    raw_lc     TEXT NOT NULL,         -- normalised raw (lower, trimmed)
    canon_id   TEXT NOT NULL,         -- MeSH id or ART-{T,C,X}-#####
    canon_name TEXT NOT NULL,
    n_uses     INTEGER NOT NULL DEFAULT 1,
    first_seen REAL NOT NULL,
    last_used  REAL NOT NULL,
    PRIMARY KEY (gse, col, raw_lc)
);
CREATE INDEX IF NOT EXISTS idx_p2canon_gse_col
    ON gse_phase2_canon(gse, col);
"""

_KEY_RE = re.compile(r"\s*([^:|;]+):", re.UNICODE)


def _char_keys(characteristics: Optional[str]) -> List[str]:
    """Sorted unique key names from a `characteristics_ch1`-style string."""
    if not characteristics:
        return []
    keys = set()

    for part in re.split(r"[;|]", characteristics):
        m = _KEY_RE.match(part)
        if m:
            keys.add(m.group(1).strip().lower())
    return sorted(keys)


def _is_ns(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("", "not specified", "none", "n/a", "na")


class GSEContextCache:
    """Thread-safe sqlite cache for per-(GSE, GSM) context + per-phase labels.

    Read APIs return plain dicts. Write APIs upsert and refresh the
    relevant ``gse_field_aggregate`` rows in the same transaction.
    """

    def __init__(self, path: str = _DEFAULT_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._con: Optional[sqlite3.Connection] = None

        self._dirty_agg: set = set()
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._con = sqlite3.connect(
                path, check_same_thread=False, isolation_level=None
            )
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute("PRAGMA synchronous=NORMAL")
            self._con.row_factory = sqlite3.Row
            self._con.executescript(_SCHEMA)
        except Exception as e:
            print(f"[gse_context_cache] disk cache disabled: {e !r}", flush=True)
            self._con = None

    def upsert_context(self, gse: str, gsm: str, raw: Dict[str, str]) -> None:
        if self._con is None:
            return
        keys = _char_keys(raw.get("characteristics") or raw.get("characteristics_ch1"))
        with self._lock:
            self._con.execute(
                "INSERT INTO gsm_context (gse, gsm, title, source_name, "
                "characteristics, treatment_protocol, description, "
                "char_keys, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(gse, gsm) DO UPDATE SET "
                "  title=excluded.title, "
                "  source_name=excluded.source_name, "
                "  characteristics=excluded.characteristics, "
                "  treatment_protocol=excluded.treatment_protocol, "
                "  description=excluded.description, "
                "  char_keys=excluded.char_keys, "
                "  updated_at=excluded.updated_at",
                (
                    gse,
                    gsm,
                    raw.get("title") or raw.get("gsm_title") or "",
                    raw.get("source_name") or raw.get("source_name_ch1") or "",
                    raw.get("characteristics") or raw.get("characteristics_ch1") or "",
                    raw.get("treatment_protocol")
                    or raw.get("treatment_protocol_ch1")
                    or "",
                    raw.get("description") or "",
                    json.dumps(keys),
                    time.time(),
                ),
            )

    def get_context(self, gse: str, gsm: str) -> Optional[dict]:
        if self._con is None:
            return None
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM gsm_context WHERE gse=? AND gsm=?", (gse, gsm)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["char_keys"] = json.loads(d["char_keys"] or "[]")
        return d

    def list_gsms_in_gse(self, gse: str) -> List[str]:
        if self._con is None:
            return []
        with self._lock:
            return [
                r["gsm"]
                for r in self._con.execute(
                    "SELECT gsm FROM gsm_context WHERE gse=?", (gse,)
                )
            ]

    def upsert_phase_value(
        self, gse: str, gsm: str, field: str, phase: str, value: Optional[str]
    ) -> None:
        """``phase`` ∈ {'p1','p1b','p1c','p2'}.

        Atomic: single INSERT … ON CONFLICT DO UPDATE writes both the row
        creation and the column update in one statement, so a concurrent
        reader never observes a half-initialised row.
        """
        if self._con is None:
            return
        if phase not in ("p1", "p1b", "p1c", "p2"):
            raise ValueError(f"unknown phase {phase !r}")
        if field not in ALL_LABEL_COLS:
            raise ValueError(f"unknown field {field !r}")
        col = phase + "_value"

        sql = (
            f"INSERT INTO gsm_phase_results (gse, gsm, field, {col}, "
            f"  updated_at) VALUES (?,?,?,?,?) "
            f"ON CONFLICT(gse, gsm, field) DO UPDATE SET "
            f"  {col}=excluded.{col}, updated_at=excluded.updated_at"
        )
        with self._lock:
            self._con.execute(sql, (gse, gsm, field, value, time.time()))

            if phase in ("p1", "p1b"):
                self._dirty_agg.add((gse, field))

    def get_phase_results(self, gse: str, gsm: str) -> Dict[str, Dict[str, str]]:
        """Returns {field: {p1, p1b, p1c, p2}}."""
        if self._con is None:
            return {}
        with self._lock:
            rows = self._con.execute(
                "SELECT field, p1_value, p1b_value, p1c_value, p2_value "
                "FROM gsm_phase_results WHERE gse=? AND gsm=?",
                (gse, gsm),
            ).fetchall()
        return {
            r["field"]: {
                "p1": r["p1_value"] or NS,
                "p1b": r["p1b_value"] or NS,
                "p1c": r["p1c_value"] or NS,
                "p2": r["p2_value"] or NS,
            }
            for r in rows
        }

    def _refresh_aggregate(
        self, gse: str, field: str, prefer_phase: str = "p1b"
    ) -> None:
        """Recompute the value distribution for one (gse, field).

        Uses ``prefer_phase`` value for each GSM if present, else falls
        back to ``p1`` value. Counts NS, non-NS, and produces a value
        histogram. Also gathers the set of ``char_keys`` for each GSM.

        Bucket key is the original casing of the *most common* form
        seen for each lowercase value — preserves canonical
        capitalisation while merging case variants ("Lung"/"lung").
        """
        if self._con is None:
            return
        if prefer_phase not in ("p1", "p1b", "p1c", "p2"):
            raise ValueError(f"unknown prefer_phase {prefer_phase !r}")
        col = prefer_phase + "_value"

        rows = self._con.execute(
            f"SELECT pr.gsm, pr.{col} AS pref_v, pr.p1_value, c.char_keys "
            f"FROM gsm_phase_results pr "
            f"LEFT JOIN gsm_context c ON c.gse=pr.gse AND c.gsm=pr.gsm "
            f"WHERE pr.gse=? AND pr.field=?",
            (gse, field),
        ).fetchall()
        if not rows:
            return
        n_total = len(rows)
        n_ns = 0

        by_lc: Dict[str, Dict[str, int]] = {}
        struct_keys: List[List[str]] = []
        for r in rows:
            v = r["pref_v"] if r["pref_v"] is not None else r["p1_value"]
            if _is_ns(v):
                n_ns += 1
                by_lc.setdefault(NS.lower(), {}).setdefault(NS, 0)
                by_lc[NS.lower()][NS] += 1
            else:
                orig = v.strip()
                lc = orig.lower()
                by_lc.setdefault(lc, {})
                by_lc[lc][orig] = by_lc[lc].get(orig, 0) + 1
            try:
                struct_keys.append(json.loads(r["char_keys"] or "[]"))
            except Exception:
                struct_keys.append([])

        dist: Dict[str, int] = {}
        for lc, casings in by_lc.items():
            canon = max(casings.items(), key=lambda kv: kv[1])[0]
            dist[canon] = sum(casings.values())
        self._con.execute(
            "INSERT INTO gse_field_aggregate (gse, field, n_total, n_ns, "
            "value_dist, struct_keys, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(gse, field) DO UPDATE SET "
            "  n_total=excluded.n_total, n_ns=excluded.n_ns, "
            "  value_dist=excluded.value_dist, "
            "  struct_keys=excluded.struct_keys, "
            "  updated_at=excluded.updated_at",
            (
                gse,
                field,
                n_total,
                n_ns,
                json.dumps(dist),
                json.dumps(struct_keys),
                time.time(),
            ),
        )

    def _ensure_aggregate_fresh(
        self, gse: str, field: str, prefer_phase: str = "p1b"
    ) -> None:
        if (gse, field) in self._dirty_agg:
            self._refresh_aggregate(gse, field, prefer_phase)
            self._dirty_agg.discard((gse, field))

    def flush_aggregates(
        self, gse: Optional[str] = None, prefer_phase: str = "p1b"
    ) -> int:
        """Refresh all dirty aggregates (optionally restricted to ``gse``).

        Returns the number of (gse, field) pairs refreshed.
        """
        if self._con is None or not self._dirty_agg:
            return 0
        with self._lock:
            pending = [p for p in self._dirty_agg if gse is None or p[0] == gse]
            for g, f in pending:
                self._refresh_aggregate(g, f, prefer_phase)
                self._dirty_agg.discard((g, f))
        return len(pending)

    def get_aggregate(self, gse: str, field: str) -> Optional[dict]:
        if self._con is None:
            return None
        with self._lock:

            self._ensure_aggregate_fresh(gse, field)
            row = self._con.execute(
                "SELECT * FROM gse_field_aggregate WHERE gse=? AND field=?",
                (gse, field),
            ).fetchone()
        if not row:
            return None
        return {
            "n_total": row["n_total"],
            "n_ns": row["n_ns"],
            "value_dist": json.loads(row["value_dist"]),
            "struct_keys": json.loads(row["struct_keys"] or "[]"),
        }

    def consensus_verdict(
        self, gse: str, gsm: str, field: str, value: str, threshold: float = 0.80
    ) -> dict:
        """Universal sibling-consensus check.

        Returns a verdict dict::

            {action:  'demote_to_NS' | 'promote_to_majority'
                     | 'keep' | 'no_data',
             reason:  short string,
             majority_value: <str or None>,
             support: float in [0,1],
             value_grounded_in_self_metadata: bool,
             struct_twin_count: int}

        ``demote_to_NS``:
            value != NS, but ≥``threshold`` of siblings (in this GSE,
            same field) are NS, AND the value's lowercase form is *not*
            a substring of THIS GSM's own raw metadata blob (i.e. the
            value didn't come from this sample).

        ``promote_to_majority``:
            value == NS, but ≥``threshold`` of siblings agree on a
            single non-NS value.

        Otherwise ``keep``.
        """
        agg = self.get_aggregate(gse, field)
        if not agg or agg["n_total"] < 3:
            return {
                "action": "no_data",
                "reason": "insufficient_siblings",
                "majority_value": None,
                "support": 0.0,
                "value_grounded_in_self_metadata": False,
                "struct_twin_count": 0,
            }

        n = agg["n_total"]
        dist = agg["value_dist"]

        non_ns = sorted(
            ((k, c) for k, c in dist.items() if k.lower() != NS.lower()),
            key=lambda kv: -kv[1],
        )
        ns_count = dist.get(NS, agg["n_ns"])
        ns_frac = ns_count / n
        majority = non_ns[0] if non_ns else (None, 0)
        majority_frac = majority[1] / n if non_ns else 0.0

        ctx = self.get_context(gse, gsm)
        my_keys = tuple(ctx["char_keys"]) if ctx else ()
        twin = sum(1 for ks in agg["struct_keys"] if tuple(ks) == my_keys)

        grounded = False
        if ctx and value and not _is_ns(value):
            blob = " | ".join(
                filter(
                    None,
                    [
                        ctx.get("title"),
                        ctx.get("source_name"),
                        ctx.get("characteristics"),
                        ctx.get("treatment_protocol"),
                        ctx.get("description"),
                    ],
                )
            ).lower()
            grounded = value.strip().lower() in blob

        if not _is_ns(value) and ns_frac >= threshold and not grounded:
            return {
                "action": "demote_to_NS",
                "reason": f"sibling_NS_consensus_{ns_frac:.2f}_ungrounded",
                "majority_value": NS,
                "support": ns_frac,
                "value_grounded_in_self_metadata": grounded,
                "struct_twin_count": twin,
            }

        if _is_ns(value) and majority_frac >= threshold and majority[0]:
            return {
                "action": "promote_to_majority",
                "reason": f"sibling_value_consensus_{majority_frac:.2f}",
                "majority_value": majority[0],
                "support": majority_frac,
                "value_grounded_in_self_metadata": False,
                "struct_twin_count": twin,
            }

        return {
            "action": "keep",
            "reason": f"ns_frac={ns_frac:.2f} maj_frac={majority_frac:.2f}",
            "majority_value": majority[0],
            "support": max(ns_frac, majority_frac),
            "value_grounded_in_self_metadata": grounded,
            "struct_twin_count": twin,
        }

    @staticmethod
    def hash_raw(raw: Dict[str, str]) -> str:
        """Stable content hash for a Phase 1 raw input dict."""
        import hashlib

        parts = [
            raw.get("title") or raw.get("gsm_title") or "",
            raw.get("source_name") or raw.get("source_name_ch1") or "",
            raw.get("characteristics") or raw.get("characteristics_ch1") or "",
            raw.get("treatment_protocol") or raw.get("treatment_protocol_ch1") or "",
            raw.get("description") or "",
        ]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def get_phase1_episodic(
        self, raw_hash: str, field: str, model_version: str, prompt_version: str
    ) -> Optional[str]:
        if self._con is None:
            return None
        with self._lock:
            r = self._con.execute(
                "SELECT value FROM phase1_episodic "
                "WHERE raw_hash=? AND field=? AND model_version=? "
                "  AND prompt_version=?",
                (raw_hash, field, model_version, prompt_version),
            ).fetchone()
        return r["value"] if r else None

    def set_phase1_episodic(
        self,
        raw_hash: str,
        field: str,
        value: str,
        model_version: str,
        prompt_version: str,
    ) -> None:
        if self._con is None:
            return
        with self._lock:
            self._con.execute(
                "INSERT INTO phase1_episodic (raw_hash, field, value, "
                "model_version, prompt_version, updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(raw_hash, field, model_version, prompt_version) "
                "DO UPDATE SET value=excluded.value, "
                "  updated_at=excluded.updated_at",
                (raw_hash, field, value, model_version, prompt_version, time.time()),
            )

    @staticmethod
    def hash_gse_state(
        gse_meta: Dict[str, str],
        sibling_dist: Dict[str, Dict[str, int]],
        labels: Optional[Dict[str, str]] = None,
    ) -> str:
        """Stable hash of GSE-context + sibling distribution + per-GSM
        Phase-1 labels.

        Two Phase-1b calls on the same (gsm, GSE-state, Phase-1 labels)
        yield identical prompts, so the cached recovery applies. The
        per-GSM Phase-1 labels are included because Phase 1b's user
        message exposes them on the Tissue branch (tissue-from-condition
        fallback rule v5+) — so a change to this GSM's Phase-1 Condition
        / Treatment must invalidate the cached Phase-1b row even when
        the GSE-level state is unchanged.
        """
        import hashlib

        meta = "\x1f".join(
            [
                (gse_meta or {}).get("gse_title", ""),
                (gse_meta or {}).get("gse_summary", ""),
                (gse_meta or {}).get("gse_design", ""),
            ]
        )
        dist_canon = json.dumps(
            {f: sorted((sibling_dist or {}).get(f, {}).items()) for f in LABEL_COLS},
            sort_keys=True,
        )
        labels_canon = json.dumps(
            {f: (labels or {}).get(f, NS) for f in LABEL_COLS}, sort_keys=True
        )
        return hashlib.sha256(
            (meta + "\x1e" + dist_canon + "\x1e" + labels_canon).encode("utf-8")
        ).hexdigest()

    def get_phase1b_episodic(
        self,
        gsm: str,
        gse_hash: str,
        field: str,
        model_version: str,
        prompt_version: str,
    ) -> Optional[str]:
        if self._con is None:
            return None
        with self._lock:
            r = self._con.execute(
                "SELECT p1b_value FROM phase1b_episodic "
                "WHERE gsm=? AND gse_hash=? AND field=? AND model_version=? "
                "  AND prompt_version=?",
                (gsm, gse_hash, field, model_version, prompt_version),
            ).fetchone()
        return r["p1b_value"] if r else None

    def set_phase1b_episodic(
        self,
        gsm: str,
        gse_hash: str,
        field: str,
        p1_value: Optional[str],
        p1b_value: str,
        model_version: str,
        prompt_version: str,
    ) -> None:
        if self._con is None:
            return
        with self._lock:
            self._con.execute(
                "INSERT INTO phase1b_episodic (gsm, gse_hash, field, "
                "p1_value, p1b_value, model_version, prompt_version, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(gsm, gse_hash, field, model_version, "
                "prompt_version) DO UPDATE SET "
                "  p1b_value=excluded.p1b_value, "
                "  p1_value=excluded.p1_value, "
                "  updated_at=excluded.updated_at",
                (
                    gsm,
                    gse_hash,
                    field,
                    p1_value,
                    p1b_value,
                    model_version,
                    prompt_version,
                    time.time(),
                ),
            )

    def get_compressed_summary(self, gse: str) -> Optional[dict]:
        if self._con is None:
            return None
        with self._lock:
            r = self._con.execute(
                "SELECT full_hash, summary_compressed, n_chars_in, "
                "n_chars_out, generated_at FROM gse_summary_compressed "
                "WHERE gse=?",
                (gse,),
            ).fetchone()
        return dict(r) if r else None

    def set_compressed_summary(
        self, gse: str, full_hash: str, summary: str, n_chars_in: int
    ) -> None:
        if self._con is None:
            return
        with self._lock:
            self._con.execute(
                "INSERT INTO gse_summary_compressed (gse, full_hash, "
                "summary_compressed, n_chars_in, n_chars_out, generated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(gse) DO UPDATE SET "
                "  full_hash=excluded.full_hash, "
                "  summary_compressed=excluded.summary_compressed, "
                "  n_chars_in=excluded.n_chars_in, "
                "  n_chars_out=excluded.n_chars_out, "
                "  generated_at=excluded.generated_at",
                (gse, full_hash, summary, n_chars_in, len(summary), time.time()),
            )

    def set_gse_canon(
        self, gse: str, col: str, raw_lc: str, canon_id: str, canon_name: str
    ) -> None:
        """Persist (or bump n_uses on) a per-(GSE, col, raw_lc) canonical
        decision so subsequent siblings see the same canonical id.

        ``raw_lc`` is normalised here (lower + strip); callers may pass
        the original surface form. Idempotent — duplicate calls bump
        ``n_uses`` and refresh ``last_used``.
        """
        if self._con is None:
            return
        if col not in LABEL_COLS:
            raise ValueError(f"unknown col {col !r}")
        key = (raw_lc or "").strip().lower()
        if not key or not canon_id:
            return
        now = time.time()
        with self._lock:
            self._con.execute(
                "INSERT INTO gse_phase2_canon (gse, col, raw_lc, "
                "canon_id, canon_name, n_uses, first_seen, last_used) "
                "VALUES (?,?,?,?,?,1,?,?) "
                "ON CONFLICT(gse, col, raw_lc) DO UPDATE SET "
                "  canon_id=excluded.canon_id, "
                "  canon_name=excluded.canon_name, "
                "  n_uses=gse_phase2_canon.n_uses+1, "
                "  last_used=excluded.last_used",
                (gse, col, key, canon_id, canon_name, now, now),
            )

    def get_gse_canon(self, gse: str, col: str, raw_lc: str) -> Optional[dict]:
        """Return ``{canon_id, canon_name, n_uses}`` if a canonical
        decision exists for this (GSE, col, raw_lc); else None.
        """
        if self._con is None:
            return None
        key = (raw_lc or "").strip().lower()
        if not key:
            return None
        with self._lock:
            r = self._con.execute(
                "SELECT canon_id, canon_name, n_uses "
                "FROM gse_phase2_canon "
                "WHERE gse=? AND col=? AND raw_lc=?",
                (gse, col, key),
            ).fetchone()
        return dict(r) if r else None

    def list_promote_candidates(self, min_gses: int = 3) -> List[dict]:
        """Cross-GSE consensus promotions.

        Returns ``(col, raw_lc, canon_id, canon_name, n_gses, n_uses)``
        for every (col, raw_lc) where the SAME canonical id has been
        chosen in at least ``min_gses`` distinct experiments AND no
        competing canonical id exists for that (col, raw_lc) anywhere
        in the per-GSE table. The caller then mirrors these into the
        global episodic table so future first-time studies start from
        cluster-wide consensus.

        Universal — relies only on the count of *experiments* that
        independently picked the same id; never promotes when there is
        any disagreement, so a single noisy GSE cannot poison the
        global cache.
        """
        if self._con is None:
            return []
        with self._lock:
            rows = self._con.execute(
                "SELECT col, raw_lc, canon_id, canon_name, "
                "  COUNT(DISTINCT gse) AS n_gses, "
                "  SUM(n_uses)         AS n_uses "
                "FROM gse_phase2_canon "
                "GROUP BY col, raw_lc, canon_id, canon_name "
                "HAVING n_gses >= ?",
                (min_gses,),
            ).fetchall()

            conflict = {
                (r["col"], r["raw_lc"])
                for r in self._con.execute(
                    "SELECT col, raw_lc FROM gse_phase2_canon "
                    "GROUP BY col, raw_lc "
                    "HAVING COUNT(DISTINCT canon_id) > 1"
                ).fetchall()
            }
        return [dict(r) for r in rows if (r["col"], r["raw_lc"]) not in conflict]

    def list_gse_canon(self, gse: str, col: str) -> List[dict]:
        """All per-GSE canonical entries for one column, most-used first.

        Used by the morphology / token-overlap tier in Phase 2 to decide
        if a fresh raw is a paraphrase of an already-resolved sibling.
        """
        if self._con is None:
            return []
        with self._lock:
            rows = self._con.execute(
                "SELECT raw_lc, canon_id, canon_name, n_uses "
                "FROM gse_phase2_canon WHERE gse=? AND col=? "
                "ORDER BY n_uses DESC, last_used DESC",
                (gse, col),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        if self._con is not None:
            with self._lock:
                self._con.close()
                self._con = None
