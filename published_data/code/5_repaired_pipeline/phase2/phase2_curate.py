"""Curation: the final stage of normalization.

Normalization assigns each distinct label a vocabulary target. Curation reviews
those assignments and produces the labels that ship. It reads the finished
dictionary rather than the corpus, so it costs one decision per distinct value
rather than one per sample, and never re-runs extraction.

Three properties make it safe to let a reviewer rewrite finished output:

  confirm before changing   Keeping an assignment is free; changing one requires
                            a second, independent review at a larger reasoning
                            budget that reaches the same conclusion. A single
                            opinion can be wrong, and an unconfirmed rewrite
                            would trade one error for another.

  effort follows impact     A value used in forty thousand samples is always
                            re-reviewed; a value used once is reviewed once
                            unless its reviewer wanted a change. Review effort
                            is allocated by how many samples a decision affects.

  cluster consistency       Individually plausible assignments can still be
                            mutually inconsistent. Every label reaching the same
                            target is examined together, and members that do not
                            belong are removed.

Nothing here encodes a list of known-bad examples. The reviewer sees the label,
its current target and the vocabulary neighbourhood, and judges from those, so
the stage applies unchanged to any corpus.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import phase2_llm

NS = "Not Specified"

ALWAYS_CONFIRM_COUNT = int(os.environ.get("CURATE_CONFIRM_COUNT", "50"))
CONFIRM_THINK_BUDGET = int(os.environ.get("CURATE_CONFIRM_THINK", "1024"))

_SYS_VALUE = (
    "You review how a biomedical sample label was normalized to a controlled "
    "vocabulary, and decide whether the result should ship.\n"
    "\n"
    "You are given the field, the label, the target it currently maps to, and "
    "candidate terms retrieved for that label.\n"
    "\n"
    "Principles:\n"
    "- The target must denote the SAME concept as the label. Prefer the most "
    "specific candidate that fully covers it.\n"
    "- A cell-line identifier must map to that cell line. A descriptive phrase "
    "is not a cell line and must not map to one.\n"
    "- An anatomical label takes an anatomical target; a disease label takes a "
    "disease target; an administered substance takes a substance target.\n"
    "- Answer OOV only when no candidate denotes the concept.\n"
    "- Answer NONE when the label asserts nothing: nothing administered, a zero "
    "dose, or no statement about the sample.\n"
    "- Answer KEEP when the current target is already correct.\n"
    "\n"
    'Reply JSON only: {"action":"KEEP"|"REPLACE"|"OOV"|"NONE",'
    '"choice":<candidate index or -1>,"confidence":<0-1>}'
)

_SYS_CLUSTER = (
    "You review a group of biomedical labels that were all normalized to the "
    "same controlled-vocabulary term.\n"
    "Identify any listed label that does NOT denote that term's concept.\n"
    "A label that is narrower than the term, or phrases it differently, DOES "
    "belong. List a label only if it denotes a different concept.\n"
    "\n"
    'Reply JSON only: {"wrong":[<indices>],"confidence":<0-1>}'
)


_SYS_ASSIGNED = (
    "You check whether a biomedical sample label was normalized to the correct "
    "controlled-vocabulary term.\n"
    "\n"
    "Principles:\n"
    "- The term must denote the SAME concept as the label. A term broader than "
    "the label is acceptable when no candidate is more specific.\n"
    "- A cell-line identifier must map to that cell line. A descriptive phrase "
    "must not map to a cell line.\n"
    "- If the label is a cell-line identifier already mapped to that cell "
    "line, KEEP it. Do not move it to a term that merely names the line.\n"
    "- An anatomical label takes an anatomical term; a disease label a disease "
    "term; an administered substance a substance term.\n"
    "- If the label denotes cells, the target must be a cell type or a cell "
    "line. An organ, a tissue or a body region is not an acceptable target "
    "for a label that names cells.\n"
    "- KEEP if the current term is correct.\n"
    "- REPLACE only if a listed candidate denotes the concept better.\n"
    "- NONE only if the label asserts nothing at all: nothing administered, a "
    "zero dose, or no statement about the sample.\n"
    "- If the current term is wrong and no candidate fits, answer WRONG. Do not "
    "guess and do not discard the label.\n"
    "\n"
    'Reply JSON only: {"action":"KEEP"|"REPLACE"|"NONE"|"WRONG",'
    '"choice":<candidate index or -1>,"confidence":<0-1>}'
)

_SYS_UNASSIGNED = (
    "A biomedical sample label was recorded as absent from the controlled "
    "vocabulary. Check whether the vocabulary in fact defines it.\n"
    "\n"
    "Principles:\n"
    "- ASSIGN a candidate if it denotes the label's concept, including when it "
    "phrases it differently or is somewhat broader.\n"
    "- A cell-line identifier must map to that cell line.\n"
    "- If the label denotes cells, the target must be a cell type or a cell "
    "line. An organ, a tissue or a body region is not an acceptable target "
    "for a label that names cells.\n"
    "- KEEP the absent status only if no candidate denotes the concept.\n"
    "- NONE if the label asserts nothing at all.\n"
    "\n"
    'Reply JSON only: {"action":"ASSIGN"|"KEEP"|"NONE",'
    '"choice":<candidate index or -1>,"confidence":<0-1>}'
)


def _schema_for(assigned: bool, n: int) -> dict:
    actions = (
        ["KEEP", "REPLACE", "NONE", "WRONG"] if assigned else ["ASSIGN", "KEEP", "NONE"]
    )
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": actions},
            "choice": {"type": "integer", "minimum": -1, "maximum": max(n - 1, 0)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["action", "choice", "confidence"],
        "additionalProperties": False,
    }


def _identifier_shaped(text: str) -> bool:
    """Whether a label looks like a cell-line accession rather than a phrase."""
    t = (text or "").strip()
    if not t:
        return False
    if any(ch.isdigit() for ch in t):
        return True
    return len(t.split()) == 1


def _is_cell_identity(entry: dict, raw: str) -> bool:
    """A label that is an identifier and already resolves to a cell line."""
    return str(entry.get("id") or "").startswith("CVCL") and _identifier_shaped(
        entry.get("canon") or raw
    )


def _has_target(entry: dict) -> bool:
    t = entry.get("target")
    return bool(t) and t != NS


def _value_schema(n: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["KEEP", "REPLACE", "OOV", "NONE"]},
            "choice": {"type": "integer", "minimum": -1, "maximum": max(n - 1, 0)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["action", "choice", "confidence"],
        "additionalProperties": False,
    }


_CLUSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "wrong": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["wrong", "confidence"],
    "additionalProperties": False,
}


_FAILURES = Counter()
_FAILED_VALUES: list = []
_FAIL_LOCK = threading.Lock()


def _note_failure(reason: str) -> None:
    with _FAIL_LOCK:
        _FAILURES[reason] += 1


def failure_report() -> tuple:
    with _FAIL_LOCK:
        return dict(_FAILURES), list(_FAILED_VALUES)


def _record_unreviewed(col: str, raw: str) -> None:
    with _FAIL_LOCK:
        _FAILED_VALUES.append((col, raw))


def _ask(
    system: str, user: str, schema: dict, think_budget: int | None = None
) -> dict | None:
    body = {
        "model": phase2_llm.MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "seed": 0,
        "max_tokens": phase2_llm.MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "review", "schema": schema},
        },
    }
    if phase2_llm.THINK:
        body["chat_template_kwargs"] = {"enable_thinking": True}
        b = think_budget if think_budget is not None else phase2_llm.THINK_BUDGET
        if b:
            body["thinking_token_budget"] = b
    try:
        txt, _fin = phase2_llm._post(body)
    except Exception as exc:
        _note_failure("request:" + type(exc).__name__)
        return None
    if not txt:
        _note_failure("empty-response")
        return None
    try:
        return json.loads(txt[txt.index("{") : txt.rindex("}") + 1])
    except (ValueError, IndexError):
        _note_failure("unparseable")
        return None


def _value_prompt(col: str, raw: str, entry: dict, cands) -> str:
    cur = entry.get("target") or (
        "out-of-vocabulary"
        if entry.get("stage", "").endswith("OOV") or entry.get("stage") == "minted"
        else NS
    )
    lines = [f"[{i}] {c.name}" for i, c in enumerate(cands)] or ["(none retrieved)"]
    return (
        f'Field: {col}\nLabel: "{raw}"\nCurrently mapped to: "{cur}"\n\n'
        "Candidates:\n" + "\n".join(lines)
    )


def _candidate_cache_path(out_dir: str) -> str:
    return os.path.join(out_dir, "candidates.cache.pkl")


def _cache_fingerprint(index_path: str, n_values: int) -> tuple:
    """Identity of the inputs the cached candidates were derived from."""
    try:
        st = os.stat(index_path)
        return (os.path.basename(index_path), st.st_size, int(st.st_mtime), n_values)
    except OSError:
        return ("", 0, 0, n_values)


def load_candidates(out_dir: str, index_path: str, n_values: int):
    """Return cached candidate lists when they match the current inputs."""
    path = _candidate_cache_path(out_dir)
    if not os.path.exists(path):
        return None
    try:
        import pickle

        with open(path, "rb") as fh:
            blob = pickle.load(fh)
    except Exception:
        return None
    if blob.get("fingerprint") != _cache_fingerprint(index_path, n_values):
        return None
    return blob.get("candidates")


def save_candidates(out_dir: str, index_path: str, n_values: int, cand: dict) -> None:
    import pickle

    path = _candidate_cache_path(out_dir)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(
            {
                "fingerprint": _cache_fingerprint(index_path, n_values),
                "candidates": cand,
            },
            fh,
            protocol=4,
        )
    os.replace(tmp, path)


def review_values(
    res: dict,
    retr,
    cols,
    workers: int = 256,
    topk: int = 8,
    log=print,
    checkpoint=None,
    worklist=None,
    cache_dir: str = "",
    index_path: str = "",
) -> Counter:
    """Pass A. Re-decide every value; apply a change only once confirmed."""
    todo = (
        worklist if worklist is not None else [(c, raw) for c in cols for raw in res[c]]
    )
    log(
        f"curation A: reviewing {len(todo):,} values "
        f"(confirm changes; always re-review at >={ALWAYS_CONFIRM_COUNT} samples)"
    )

    cand = load_candidates(cache_dir, index_path, len(todo)) if cache_dir else None
    if cand is not None:
        log(f"  reusing cached candidates for {len(cand):,} values")
        return_early_cand = True
    else:
        cand = {}
        return_early_cand = False
    t0 = time.time()
    for c in ([] if return_early_cand else cols):
        raws = [raw for cc, raw in todo if cc == c]
        for i in range(0, len(raws), 4096):
            chunk = raws[i : i + 4096]
            qs = [res[c][r].get("canon") or r for r in chunk]
            for r, cs in zip(chunk, retr.search(qs, c, k=topk)):
                cand[(c, r)] = cs
    if not return_early_cand:
        log(
            f"  retrieved candidates for {len(cand):,} values "
            f"({time.time()-t0 :.0f}s)"
        )
        if cache_dir:
            save_candidates(cache_dir, index_path, len(todo), cand)

    stats = Counter()
    changes = []
    lock = threading.Lock()
    done = [0]
    t0 = time.time()

    def apply(entry, verdict, cands, stage, source, raw=""):
        act = verdict.get("action")
        if _is_cell_identity(entry, raw):
            if act not in ("REPLACE", "ASSIGN"):
                return False
            i = verdict.get("choice", -1)
            if not (0 <= i < len(cands)) or cands[i].kind != "cellosaurus":
                return False
        if act in ("REPLACE", "ASSIGN"):
            i = verdict.get("choice", -1)
            if not (0 <= i < len(cands)):
                return False
            c = cands[i]
            entry.update(
                {
                    "target": c.name,
                    "id": c.mesh_id,
                    "curated": True,
                    "curated_action": act.lower(),
                    "source": source
                    + ("-cellosaurus" if c.kind == "cellosaurus" else "-mesh"),
                    "confidence": verdict.get("confidence", 0),
                }
            )
        elif act == "WRONG":
            entry["curated_flag"] = "no-suitable-candidate"
            return False
        elif act == "NONE":
            entry.update(
                {
                    "target": NS,
                    "id": "",
                    "curated": True,
                    "curated_action": "none",
                    "source": source + "-none",
                    "confidence": verdict.get("confidence", 0),
                }
            )
        else:
            return False
        return True

    def one(item):
        col, raw = item
        entry = res[col][raw]
        cands = cand.get((col, raw), [])
        n = entry.get("count", 1)
        before = entry.get("target")

        assigned = _has_target(entry)
        sysmsg = _SYS_ASSIGNED if assigned else _SYS_UNASSIGNED
        schema = _schema_for(assigned, len(cands))
        first = _ask(sysmsg, _value_prompt(col, raw, entry, cands), schema)
        with lock:
            done[0] += 1
            if done[0] % 5000 == 0:
                log(
                    f"  A {done[0]:,}/{len(todo):,} "
                    f"({done[0]/max(time.time()-t0 ,1e-9):.0f}/s)"
                )
                if checkpoint:
                    checkpoint(res)
        if first is None:
            _record_unreviewed(col, raw)
            with lock:
                stats["review-failed"] += 1
            return

        wants_change = first.get("action") != "KEEP"
        high_impact = n >= ALWAYS_CONFIRM_COUNT
        if not wants_change and not high_impact:
            with lock:
                stats["kept"] += 1
            return

        second = _ask(
            sysmsg,
            _value_prompt(col, raw, entry, cands),
            schema,
            think_budget=CONFIRM_THINK_BUDGET,
        )
        with lock:
            if second is None:
                _record_unreviewed(col, raw)
                stats["confirm-failed"] += 1
                return
            same = second.get("action") == first.get("action") and (
                first.get("action") != "REPLACE"
                or second.get("choice") == first.get("choice")
            )
            if not wants_change:
                if second.get("action") == "KEEP":
                    stats["kept-confirmed"] += 1
                else:
                    stats["kept-disputed"] += 1
                return
            if not same:
                stats["change-unconfirmed"] += 1
                return
            if apply(entry, second, cands, "reviewed", "curated", raw):
                stats["changed-" + second["action"].lower()] += 1
                changes.append((n, col, raw, before, entry.get("target")))
            else:
                stats["change-invalid"] += 1

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, todo))
    log(f"curation A done in {time.time()-t0 :.0f}s — {dict(stats)}")
    changes.sort(reverse=True)
    log("  highest-impact corrections:")
    for n, col, raw, before, after in changes[:15]:
        log(
            f"    x{n :<6d} {col :<10} {str(raw)[:32]:<32} {str(before)[:24]} -> {after}"
        )
    if checkpoint:
        checkpoint(res)
    return stats


def review_clusters(
    res: dict,
    cols,
    workers: int = 256,
    max_members: int = 40,
    log=print,
    checkpoint=None,
) -> Counter:
    """Pass B. Remove members that do not denote their cluster's concept."""
    clusters = defaultdict(list)
    for c in cols:
        for raw, r in res[c].items():
            t = r.get("target")
            if t and t != NS:
                clusters[(c, t)].append(raw)
    groups = [(c, t, m) for (c, t), m in clusters.items() if len(m) > 1]
    log(
        f"curation B: {len(groups):,} clusters covering "
        f"{sum(len(m) for _ ,_ ,m in groups):,} values"
    )

    stats = Counter()
    lock = threading.Lock()
    done = [0]
    t0 = time.time()

    def one(group):
        col, target, members = group
        members = sorted(members, key=lambda m: -res[col][m].get("count", 0))[
            :max_members
        ]
        lines = [f"[{i}] {m}" for i, m in enumerate(members)]
        user = (
            f'Field: {col}\nAll labels normalized to: "{target}"\n\n'
            + "\n".join(lines)
            + "\n\nWhich do not denote that concept?"
        )
        first = _ask(_SYS_CLUSTER, user, _CLUSTER_SCHEMA)
        with lock:
            done[0] += 1
            if done[0] % 2000 == 0:
                log(
                    f"  B {done[0]:,}/{len(groups):,} "
                    f"({done[0]/max(time.time()-t0 ,1e-9):.0f}/s)"
                )
                if checkpoint:
                    checkpoint(res)
        if first is None:
            with lock:
                stats["cluster-failed"] += 1
            return
        flagged = [i for i in first.get("wrong", []) if 0 <= i < len(members)]
        if not flagged:
            with lock:
                stats["clusters-clean"] += 1
            return
        second = _ask(
            _SYS_CLUSTER, user, _CLUSTER_SCHEMA, think_budget=CONFIRM_THINK_BUDGET
        )
        with lock:
            stats["clusters-flagged"] += 1
            if second is None:
                stats["eject-unconfirmed"] += 1
                return
            confirmed = set(flagged) & {
                i for i in second.get("wrong", []) if 0 <= i < len(members)
            }
            for i in sorted(confirmed):
                r = res[col][members[i]]
                r["curated_flag"] = "cluster-outlier"
                stats["flagged-outlier"] += 1
            stats["eject-unconfirmed"] += len(set(flagged) - confirmed)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, groups))
    log(f"curation B done in {time.time()-t0 :.0f}s — {dict(stats)}")
    if checkpoint:
        checkpoint(res)
    return stats


def report(res: dict, cols, log=print) -> None:
    """Summarise by normalization stage and by whether review altered a value."""
    """Summarise the curated dictionary by value and by sample impact."""
    log("curation summary (share of sample-label instances):")
    for c in cols:
        stages = Counter()
        inst = Counter()
        for r in res[c].values():
            stages[r.get("stage")] += 1
            inst[r.get("stage")] += r.get("count", 0)
        total = sum(inst.values()) or 1
        log(
            f"  {c :<10} "
            + "  ".join(
                f"{t}={stages[t]:,}({100 * inst[t]/total :.0f}%)"
                for t in sorted(stages, key=str)
            )
        )
