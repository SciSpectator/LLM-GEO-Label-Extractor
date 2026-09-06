"""Phase 2 — dictionary-based label normalization.
A dictionary of distinct label values is built across the whole corpus, each value
is normalized exactly once from its own text, and the finished dictionary is then
applied to every sample as a lookup. Deciding a value once and applying it
everywhere makes the corpus internally consistent by construction, and collapses
the work by the ratio of label instances to distinct values.
Values are judged without series context. A study routinely contains samples with
different labels -- diseased and healthy, treated and untreated -- so evidence
drawn from a sample's siblings pulls distinct values onto a single term.
Nothing is carried between runs. Each run rebuilds its dictionary from the corpus
it is given, and publishes that dictionary alongside the output as the record of
what was decided.
STAGES
------
  junk        uninformative strings never reach a model
  canonical   surface-form reduction (filler, plurals, punctuation)
  exact       exact match against the reference vocabulary
  model       retrieved candidates, selected by the model
  minted      a local identifier when no vocabulary covers the concept
Exact matching resolves a small minority of distinct values, because sample
metadata is written as lab shorthand while the vocabularies store formal headings.
The model is therefore the engine of this stage rather than an escalation path;
the deterministic stages exist to keep it from spending effort on what is
mechanically decidable.
"""

from __future__ import annotations
import gzip
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

NS = "Not Specified"
LABEL_COLS = ("Tissue", "Condition", "Treatment")
CANONICALIZER_VERSION = "w1-2"
_NONWORD = re.compile(r"[^a-z0-9%+\- ]+")
_WS = re.compile(r"\s+")
_BRACKETED = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}")
_ACCESSION = re.compile(r"\b(gsm|gse|srs|srr|srx|prjna)\d+\b", re.I)
_REPLICATE = re.compile(
    r"\b(rep(licate)?|donor|subject|patient|sample)[ _-]?\d+\b", re.I
)
_TRAILING_N = re.compile(r"\bn\s*=\s*\d+\b", re.I)
_CELL_LINE_PHRASE = re.compile(r"\bcell\s+lines?\b")
_FILLER = {
    "tissue",
    "tissues",
    "sample",
    "samples",
    "specimen",
    "specimens",
    "derived",
    "from",
    "obtained",
    "isolated",
    "purified",
    "sorted",
    "primary",
    "fresh",
    "frozen",
    "ffpe",
    "biopsy",
    "biopsies",
    "total",
    "whole",
    "human",
    "mouse",
    "rat",
    "patient",
    "patients",
    "donor",
    "donors",
    "subject",
    "subjects",
    "adult",
    "na",
    "unknown",
}
_GREEK = {
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "ε": "epsilon",
    "κ": "kappa",
    "λ": "lambda",
    "μ": "mu",
    "σ": "sigma",
    "τ": "tau",
    "ω": "omega",
}


def looks_like_short(raw: str) -> bool:
    """Is this label written in the shape of an abbreviation?
    A short form cannot be expanded from a fixed table: the same letters denote
    different concepts in different studies, so any table encodes one study's
    sense and imposes it on every other. What a short denotes is recoverable
    only from the text of the study that used it.
    This test therefore decides *who resolves the label*, never what it means.
    Being wrong is harmless in both directions -- a full name routed here is
    simply confirmed against its own context, and a short that slips past is
    handled as an ordinary value. Only surface shape is examined, so the rule
    carries no assumption about which fields or which vocabularies a corpus
    uses.
    Digits are treated as disqualifying because an identifier-shaped string is a
    cell line or a catalogue code, whose identity is the string itself.
    A plural written by adding a lowercase s to an otherwise capitalised form is
    the same abbreviation, and must take the same route as its singular, or the
    two spellings of one label resolve by different means.
    """
    s = str(raw or "").strip()
    if not s or " " in s:
        return False
    if s[-1:] == "s" and s[:-1].isupper():
        s = s[:-1]
    if any(c.islower() for c in s) or any(c.isdigit() for c in s):
        return False
    return s.isalpha() and 2 <= len(s) <= 6


def canonicalize(raw: str) -> str:
    """Deterministic surface-form reduction. Order is part of the version.

    Both the corpus value and the vocabulary term pass through this, so the
    vocabulary is already indexed under whatever form the reduction produces.
    Singularising on top of that earns almost nothing and mangles the words it
    guesses wrong -- squamous, metastasis, cirrhosis, testis all lose their last
    letter and stop matching anything, including as a retrieval query.
    """
    return _reduce(raw, drop_brackets=True)


def _reduce(raw: str, drop_brackets: bool = True) -> str:
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    for g, a in _GREEK.items():
        s = s.replace(g, a)
    s = _ACCESSION.sub(" ", s)
    s = _TRAILING_N.sub(" ", s)
    if drop_brackets:
        s = _BRACKETED.sub(" ", s)
    s = _REPLICATE.sub(" ", s)
    s = _NONWORD.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    if not s:
        return ""
    s = _CELL_LINE_PHRASE.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    toks = s.split()
    kept = [t for t in toks if t not in _FILLER]
    if not kept:
        kept = toks
    return " ".join(kept).strip()


_JUNK_EXACT = {
    "",
    "-",
    "--",
    "na",
    "n a",
    "nan",
    "none",
    "null",
    "unknown",
    "unk",
    "tbd",
    "not applicable",
    "not specified",
    "not available",
    "not determined",
    "missing",
    "blank",
    "other",
    "others",
    "misc",
    "undetermined",
    "?",
    ".",
}
_NUMERIC = re.compile(r"^[\d\s.,;:/+-]+$")


def _looks_like_identifier(raw: str) -> bool:
    """Whether a mostly-numeric string is shaped like a catalogue identifier.

    Shape only: a bare count is one run of digits, while an identifier carries
    internal structure -- a hyphen, or a digit group beside a letter.
    """
    s = str(raw or "").strip()
    return bool(
        re.match(r"^\d+[-/]\d+$", s)
        or re.match(r"^[A-Za-z]+[-]?\d+$", s)
        or (s.isdigit() and len(s) >= 3)
    )


def is_junk(canon: str, raw: str) -> bool:
    """Uninformative strings must never consume model budget.

    A string that carries an identifier shape is never junk. Catalogue and
    cell-line names are routinely digits, and discarding them here removes a
    real value before anything can look it up.
    """
    if canon in _JUNK_EXACT or not canon:
        return True
    if _NUMERIC.match(canon) and not _looks_like_identifier(raw):
        return True
    if len(canon) == 1:
        return True
    if len(raw) > 300 and len(canon.split()) > 40:
        return True
    return False


@dataclass
class Vocab:
    """MeSH (descriptors + entry terms + supplementary concepts) + Cellosaurus."""

    mesh: dict = field(default_factory=dict)
    cells: dict = field(default_factory=dict)

    @classmethod
    def load(cls, mesh_db: str, cell_db: str) -> "Vocab":
        """Both sides of the comparison MUST pass through the same canonicalizer.
        Indexing raw vocabulary strings while canonicalizing the query silently
        loses every pair that differs only by a rule the canonicalizer applies:
        MeSH stores "Placebos", the corpus says "Placebo", and singularizing only
        the query guarantees they never meet. Terms are therefore stored under
        BOTH their plain-normalized and canonicalized keys.
        """
        v = cls()
        con = sqlite3.connect(f"file:{mesh_db}?mode=ro", uri=True)
        for term, mid, pref, cat in con.execute(
            "SELECT term, mesh_id, preferred, category FROM vocab"
        ):
            if not term:
                continue
            payload = (mid, pref, cat or "")
            plain = _WS.sub(" ", _NONWORD.sub(" ", str(term).lower())).strip()
            if plain:
                v.mesh.setdefault(plain, payload)
            canon = canonicalize(term)
            if canon:
                v.mesh.setdefault(canon, payload)
        con.close()
        con = sqlite3.connect(f"file:{cell_db}?mode=ro", uri=True)
        for name, cvcl, primary in con.execute(
            "SELECT name, cvcl, primary_name FROM cell_lines"
        ):
            for n in (name, primary):
                if not n:
                    continue
                payload = (cvcl, primary or name)
                plain = _WS.sub(" ", _NONWORD.sub(" ", str(n).lower())).strip()
                if plain:
                    v.cells.setdefault(plain, payload)
                canon = canonicalize(n)
                if canon:
                    v.cells.setdefault(canon, payload)
        con.close()
        return v

    def lookup(self, canon: str, col: str) -> dict | None:
        """An exact hit against the reference vocabulary, and nothing further.
        A string is accepted only when it names a vocabulary term and nothing
        else in the corpus's namespaces answers to it. Cell-line names are not
        resolved here at all: line names collide with ordinary anatomical and
        descriptive language, so matching one is a claim about what the label
        means rather than a lookup. Those are carried to the model, which sees
        the lines among its candidates and decides with the field in view.
        """
        in_mesh = self.mesh.get(canon)
        if in_mesh and canon not in self.cells:
            return {
                "id": in_mesh[0],
                "name": in_mesh[1],
                "source": "mesh",
                "category": in_mesh[2],
                "stage": "exact",
            }
        return None


_SPAN_SPLIT = re.compile(r"\s*;\s*")


def spans(value: str) -> list:
    """The separate concepts inside one extracted value.

    Phase 1 returns every span it found for a field joined by "; ", and each is
    its own concept: "Renal cell carcinoma; tumor" is two, not one. They have to
    be normalized separately, because asking the vocabulary for the joined
    string finds nothing -- no MeSH heading spells two diseases at once -- and
    the value is then minted as a single invented concept, losing both real
    identifiers.
    """
    if not value:
        return []
    parts = [p.strip() for p in _SPAN_SPLIT.split(value)]
    return [p for p in parts if p and p != NS]


def join_spans(values: list) -> str:
    """Rejoin resolved spans in the order they were extracted.

    Positions are preserved, empty entries included: downstream steps read the
    label list and the identifier list as parallel sequences, so dropping the
    blank left by an unresolved span would shift every later identifier onto
    the wrong label.
    """
    return "; ".join(values)


def build_dictionary(corpus_paths: list[str], phase: str = "phase1b") -> dict:
    """Collect every distinct label value across the corpus, with instance counts.
    Instance count drives all downstream prioritisation: model effort, review
    order, and QA sampling allocate by how many samples a value affects, never by
    how many distinct strings exist.
    """
    freq = {c: Counter() for c in LABEL_COLS}
    shorts: dict = {c: {} for c in LABEL_COLS}
    n = 0
    for p in corpus_paths:
        op = gzip.open if str(p).endswith(".gz") else open
        with op(p, "rt") as fh:
            d = json.load(fh)
        rows = d["samples"] if isinstance(d, dict) and "samples" in d else d
        for s in rows:
            n += 1
            vals = s.get(phase) or {}
            gse = str(s.get("gse", "")).strip()
            for c in LABEL_COLS:
                v = str(vals.get(c, "")).strip()
                if not v or v == NS:
                    continue
                # One entry per span, so each concept is decided on its own
                # evidence and the dictionary holds concepts rather than the
                # accidental combinations they were extracted in.
                for part in spans(v):
                    freq[c][part] += 1
                    if not (gse and looks_like_short(part)):
                        continue
                    slot = (
                        shorts[c]
                        .setdefault(part, {})
                        .setdefault(gse, {"count": 0, "text": ""})
                    )
                    slot["count"] += 1
                    if not slot["text"]:
                        slot["text"] = sample_context(s)
    return {
        "n_samples": n,
        "freq": {c: dict(freq[c]) for c in LABEL_COLS},
        "shorts": shorts,
    }


def sample_context(s: dict, limit: int = 700) -> str:
    """The sample's own words, for resolving a label that its value alone cannot.
    Field names are kept verbatim rather than mapped, so a corpus carrying
    different metadata fields contributes whatever it has.
    """
    parts = []
    for k in (
        "title",
        "source",
        "characteristics",
        "treatment_protocol",
        "description",
    ):
        v = str(s.get(k, "") or "").strip()
        if v:
            parts.append(f"{k}: {v}")
    return " | ".join(parts)[:limit]


def resolve_deterministic(dictionary: dict, vocab: Vocab) -> dict:
    """Run the deterministic stages. Returns per-value resolution plus the
    unresolved remainder that the model stages must handle."""
    out = {}
    stats = {c: Counter() for c in LABEL_COLS}
    for col in LABEL_COLS:
        res = {}
        for raw, count in dictionary["freq"][col].items():
            canon = canonicalize(raw)
            if is_junk(canon, raw):
                res[raw] = {
                    "stage": "junk",
                    "canon": canon,
                    "target": NS,
                    "id": "",
                    "source": "junk",
                    "count": count,
                }
                stats[col]["junk"] += 1
                stats[col]["junk instances"] += count
                continue
            if looks_like_short(raw):
                res[raw] = {
                    "stage": "pending-short",
                    "canon": canon,
                    "target": None,
                    "id": "",
                    "source": "",
                    "count": count,
                }
                stats[col]["pending-short"] += 1
                stats[col]["pending-short instances"] += count
                continue
            hit = vocab.lookup(canon, col)
            if hit:
                res[raw] = {
                    "stage": "exact",
                    "canon": canon,
                    "target": hit["name"],
                    "id": hit["id"],
                    "source": hit["source"],
                    "count": count,
                }
                stats[col][f"exact {hit['source']}"] += 1
                stats[col]["exact instances"] += count
            else:
                res[raw] = {
                    "stage": "pending",
                    "canon": canon,
                    "target": None,
                    "id": "",
                    "source": "",
                    "count": count,
                }
                stats[col]["pending"] += 1
                stats[col]["pending instances"] += count
        out[col] = res
    return {"resolutions": out, "stats": {c: dict(stats[c]) for c in LABEL_COLS}}


def annotate_vocabulary_ids(resolutions: dict, vocab: "Vocab") -> dict:
    """Record every vocabulary that recognises a value, not only the chosen one.
    A cell line is often present in both a general biomedical vocabulary and a
    cell-line registry. Selecting one target does not make the other wrong: they
    identify the same thing in different namespaces, and a downstream user may
    need either. Both are therefore carried in their own fields, keyed on the
    canonical form so the lookup is exact rather than inferred.
    """
    counts = Counter()
    for col, entries in resolutions.items():
        for rec in entries.values():
            canon = rec.get("canon") or ""
            m = vocab.mesh.get(canon)
            c = vocab.cells.get(canon)
            rec["mesh_id"] = m[0] if m else ""
            rec["mesh_name"] = m[1] if m else ""
            rec["cell_id"] = c[0] if c else ""
            rec["cell_name"] = c[1] if c else ""

            chosen, src = rec.get("id") or "", rec.get("source") or ""
            if chosen and not chosen.startswith("OOV-"):
                if "cellosaurus" in src and not rec["cell_id"]:
                    rec["cell_id"] = chosen
                    rec["cell_name"] = rec.get("cell_name") or rec.get("target") or ""
                elif "mesh" in src and not rec["mesh_id"]:
                    rec["mesh_id"] = chosen
                    rec["mesh_name"] = rec.get("mesh_name") or rec.get("target") or ""
            if m and c:
                counts[col + " both"] += 1
            elif m:
                counts[col + " mesh only"] += 1
            elif c:
                counts[col + " registry only"] += 1
    return dict(counts)


def apply_dictionary(
    corpus_path: str,
    resolutions: dict,
    out_path: str,
    phase_in: str = "phase1b",
    phase_out: str = "phase2",
    shorts: dict | None = None,
    seen: dict | None = None,
) -> dict:
    """Apply the finished dictionary to every sample. Pure lookup -- no model
    calls -- so this step is instant regardless of corpus size.
    `shorts` maps (column, value, study) to the expansion resolved for that
    study, and is consulted before the dictionary.

    Every other label is normalized exactly once for the whole corpus, keyed by
    the label itself, so two samples carrying the same label can never receive
    two different answers. A short form is the single exception: the same few
    letters name different concepts in different experiments -- AD is
    Alzheimer disease in one study and atopic dermatitis in another -- so it is
    resolved once per study, from that study's own context. That stays
    reproducible because the key is still fixed by the data: the same value in
    the same study always resolves the same way, and the expansion it produces
    is then normalized through the one global dictionary like any other label.
    """
    shorts = shorts or {}
    if seen is None:
        seen = {}
    op = gzip.open if str(corpus_path).endswith(".gz") else open
    with op(corpus_path, "rt") as fh:
        d = json.load(fh)
    rows = d["samples"] if isinstance(d, dict) and "samples" in d else d
    counts = Counter()
    for s in rows:
        src = s.get(phase_in) or {}
        gse = str(s.get("gse", "")).strip()
        p2, ids, mesh_ids, cell_ids = {}, {}, {}, {}
        stages, curated = {}, {}
        for c in LABEL_COLS:
            raw = str(src.get(c, "")).strip()
            if not raw or raw == NS:
                p2[c] = NS
                counts["passthrough NS"] += 1
                continue
            # Each span carries its own decision; the cell is the list of them
            # in the order they were extracted. De-duplication of spans that
            # landed on the same concept is phase2_dedup's job.
            out_t, out_i, out_m, out_c, out_s = [], [], [], [], []
            for part in spans(raw) or [raw]:
                lookup = shorts.get((c, part, gse), part)
                r = resolutions.get(c, {}).get(lookup)
                if r is None:
                    r = resolutions.get(c, {}).get(part)
                if r is None or r.get("target") is None:
                    target = part
                    counts["unresolved kept verbatim"] += 1
                else:
                    target = r["target"]
                    counts[r["stage"]] += 1
                out_t.append(target)
                out_i.append((r or {}).get("id", ""))
                out_m.append((r or {}).get("mesh_id", ""))
                out_c.append((r or {}).get("cell_id", ""))
                out_s.append((r or {}).get("stage", ""))

                key = (c, part, gse) if looks_like_short(part) else (c, part)
                prior = seen.setdefault(key, target)
                if prior != target:
                    raise SystemExit(
                        "phase-2 reproducibility violated: the label %r in field %s "
                        "resolved to %r and to %r. A label carries one answer for the "
                        "whole corpus; a short form carries one answer per study."
                        % (part, c, target, prior)
                    )

            p2[c] = join_spans(out_t)
            stages[c] = join_spans(out_s)
            curated[c] = "yes" if (r or {}).get("curated") else "no"
            ids[c] = join_spans(out_i)
            mesh_ids[c] = join_spans(out_m)
            cell_ids[c] = join_spans(out_c)
        for c in ("Sex", "Age"):
            p2[c] = src.get(c, NS)
        s[phase_out] = p2
        s[f"{phase_out}_id"] = {c: ids.get(c, "") for c in LABEL_COLS}
        s[f"{phase_out}_mesh_id"] = {c: mesh_ids.get(c, "") for c in LABEL_COLS}
        s[f"{phase_out}_cell_id"] = {c: cell_ids.get(c, "") for c in LABEL_COLS}
        s[f"{phase_out}_stage"] = {c: stages.get(c, "") for c in LABEL_COLS}
        s[f"{phase_out}_curated"] = {c: curated.get(c, "no") for c in LABEL_COLS}
    op_out = gzip.open if str(out_path).endswith(".gz") else open
    with op_out(out_path, "wt") as fh:
        json.dump({"samples": rows, "n_samples": len(rows)}, fh, default=str)
    return dict(counts)


__all__ = [
    "canonicalize",
    "is_junk",
    "looks_like_short",
    "Vocab",
    "annotate_vocabulary_ids",
    "build_dictionary",
    "resolve_deterministic",
    "apply_dictionary",
    "CANONICALIZER_VERSION",
    "LABEL_COLS",
    "NS",
]
