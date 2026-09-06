"""Phase 2 — model selection over the pending dictionary.
Most distinct values reach the model, because sample metadata is written as lab
shorthand while the vocabularies store formal headings.
DESIGN CONSTRAINTS
------------------
* **Constrained selection.** The model receives numbered candidates and answers
  with an index or OOV. It never emits an identifier, so a fabricated vocabulary
  id is structurally impossible rather than something to filter afterwards.
* **Sample level, no series context.** A value is judged on its own text against
  real candidates. Study-level text describes a study's topic, not the state of
  any one sample within it, and is not supplied here.
* **Reasoning on.** The decision is whether a candidate genuinely denotes the same
  concept, which is a judgment rather than a lookup.
* **Dictionary-level.** Each distinct value is decided once and applied to every
  sample carrying it, so the corpus is internally consistent by construction.
"""

from __future__ import annotations
import threading
import hashlib
import itertools
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
import numpy as np

NS = "Not Specified"
VLLM_URL = os.environ.get("VLLM_URL", "http://127.0.0.1:8000/v1")
MODEL = os.environ.get("PHASE2_MODEL", "gemma4-e2b-text")
THINK = os.environ.get("PHASE2_THINK", "true").lower() in ("1", "true", "yes")
TOP_K = int(os.environ.get("PHASE2_TOPK", "8"))
MAX_TOKENS = int(os.environ.get("PHASE2_MAX_TOKENS", "1024"))
THINK_BUDGET = int(os.environ.get("PHASE2_THINK_BUDGET", "256"))
TOKEN_CEILING = int(os.environ.get("PHASE2_TOKEN_CEILING", "6144"))
GPU_QUERY_BATCH = int(os.environ.get("PHASE2_GPU_QUERY_BATCH", "256"))


@dataclass
class Candidate:
    form: str
    mesh_id: str
    name: str
    category: str
    kind: str
    score: float


def _default_device() -> str:
    """Prefer the accelerator when the host has one."""
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


EMBED_DEVICE = os.environ.get("PHASE2_EMBED_DEVICE") or _default_device()


class Retriever:
    """BioLORD nearest-neighbour over the full vocabulary index."""

    def __init__(self, index_path: str):
        z = np.load(index_path, allow_pickle=True)
        self.vecs = z["vectors"]
        self.forms = z["forms"]
        self.ids = z["ids"]
        self.names = z["names"]
        self.cats = z["categories"]
        self.kinds = z["kinds"]
        self._model = None
        self._model_lock = threading.Lock()
        self._gpu_vecs = None
        self._torch = None

    def _encode(self, texts: list[str]) -> np.ndarray:
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    m = SentenceTransformer(
                        "FremyCompany/BioLORD-2023", device=EMBED_DEVICE
                    )
                    m.max_seq_length = 64
                    self._model = m
        return self._model.encode(
            texts,
            batch_size=512,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).astype(np.float32)

    def _topk(self, q: np.ndarray, k: int):
        """The k nearest vocabulary vectors per query, as (indices, scores).

        The comparison is a dense product against the whole vocabulary and is
        the largest arithmetic step in this stage. It runs on the accelerator
        when the host has one and falls back to the array library otherwise, so
        the module still works without one. Half precision is used for the
        resident matrix because ranking depends on the ordering of cosine
        similarities rather than their exact values.
        """
        if self._gpu_vecs is None and EMBED_DEVICE.startswith("cuda"):
            try:
                import torch

                self._torch = torch
                self._gpu_vecs = torch.from_numpy(self.vecs).to(
                    EMBED_DEVICE, dtype=torch.float16
                )
            except Exception:
                self._gpu_vecs = False
        if self._gpu_vecs is not None and self._gpu_vecs is not False:
            torch = self._torch

            out_i, out_v = [], []
            step = max(1, GPU_QUERY_BATCH)
            for s0 in range(0, len(q), step):
                qt = torch.from_numpy(q[s0 : s0 + step]).to(
                    EMBED_DEVICE, dtype=torch.float16
                )
                sims = qt @ self._gpu_vecs.T
                n = min(k, sims.shape[1])
                top = torch.topk(sims, n, dim=1)
                out_i.append(top.indices.cpu().numpy())
                out_v.append(top.values.float().cpu().numpy())
                del qt, sims, top
            import numpy as _np

            return list(zip(_np.concatenate(out_i), _np.concatenate(out_v)))
        sims = q @ self.vecs.T
        rows = []
        for row in sims:
            i = np.argpartition(-row, min(k, len(row) - 1))[:k]
            i = i[np.argsort(-row[i])]
            rows.append((i, row[i]))
        return rows

    def search(
        self, queries: list[str], col: str, k: int = TOP_K
    ) -> list[list[Candidate]]:
        """The k nearest vocabulary entries, unfiltered.
        Candidates are ranked by similarity alone. Deciding in code which kinds
        of term a field may take would withhold candidates from the model
        without recording that anything was withheld, and a rule applied that
        way cannot be overridden when it is wrong: the correct answer simply
        never appears in the list. The field is stated in the prompt and the
        model applies it there, where a mistaken exclusion is visible as a
        choice rather than invisible as an absence.
        """
        q = self._encode(queries)
        out = []
        for idx, scores in self._topk(q, k * 4):
            cands, seen = [], set()
            for pos, i in enumerate(idx):
                mid = str(self.ids[i])
                if mid in seen:
                    continue
                seen.add(mid)
                cands.append(
                    Candidate(
                        str(self.forms[i]),
                        mid,
                        str(self.names[i]),
                        str(self.cats[i]),
                        str(self.kinds[i]),
                        float(scores[pos]),
                    )
                )
                if len(cands) >= k:
                    break
            out.append(cands)
        return out


_SYSTEM = (
    "You normalize biomedical sample labels to a controlled vocabulary.\n"
    "You are given ONE label, the field it belongs to, and a numbered candidate "
    "list. Choose the single candidate that denotes the SAME concept as the "
    "label.\n"
    "\n"
    "Matching:\n"
    "- Pick the most specific candidate that fully covers the label.\n"
    "- Never pick a broader term when a more specific one applies.\n"
    "- Do not pick a term that differs from the label only by a numeric variant "
    "identifier. A number beside a label is usually a stage, grade, dose or "
    "severity, and is not part of the concept's identity.\n"
    "- An abbreviation matches a term only when that term is what the "
    "abbreviation stands for. Never choose a term because it shares its "
    "letters, contains it as a code, or begins with them. When an "
    "abbreviation's meaning is not determinable from the label and its "
    "field, answer OOV rather than guess.\n"
    "- A term naming a finding, a measurement, an outcome or an observation "
    "about a condition does not denote the condition itself.\n"
    "\n"
    "The target must match the KIND of thing the label names:\n"
    "- An anatomical label takes an anatomical term.\n"
    "- A label naming CELLS takes a cell type or a cell line. An organ, a "
    "tissue or a body region is never an acceptable target for a label naming "
    "cells.\n"
    "- A label naming a disease or a physiological or experimental state takes "
    "a term for that state. This field is not limited to diseases.\n"
    "- A label naming an administered substance takes a substance term.\n"
    "\n"
    "Cell lines:\n"
    "- A cell-line identifier IS the tissue value. Keep it as that cell line; "
    "never translate it to the organ or tissue the line was derived from.\n"
    "- A descriptive phrase naming a cell type is not a cell line. Never map it "
    "to a specific cell line.\n"
    "- When a label names a cell line and the candidates include both a "
    "cell-line registry entry and a general-vocabulary term for that same "
    "line, choose the registry entry. The registry identifies the line itself, "
    "while the general vocabulary only describes it.\n"
    "- A condition comes from what the text says, never from what a cell line "
    "is known to be. A disease named in the text is that sample's condition "
    "even when it is written as part of a cell line's description. A label "
    "carrying only an identifier names no condition, and no condition may be "
    "supplied for it from knowledge of what that identifier denotes.\n"
    "\n"
    "Administration:\n"
    "- A label naming any substance given to the sample is a treatment, "
    "including a carrier, a diluent or an inactive comparator.\n"
    "- A label that names an exposure, an agent or a procedure and states that "
    "this sample did not receive it is a value in its own right, not an absence. "
    "The study is contrasting that exposure, and such a label is one side of the "
    "contrast. This holds whether the exposure was administered for the study or "
    "is a standing property of the subject.\n"
    "- A label is NONE only when it carries no concept at all. A label naming "
    "the baseline or comparison side of the design still states the condition "
    "this sample was held in, and is normalized to a term like any other value. "
    "Deciding whether that label belongs in this field is not your task: your "
    "task is to give whatever label you are handed a single consistent form.\n"
    "- A label naming a diagnosis, a finding or a measured status describes what "
    "the sample IS, not what was done to it, and is not an administration.\n"
    "\n"
    "- If no candidate denotes the same concept, answer OOV.\n"
    "\n"
    'Answer with JSON only: {"choice": <index|"OOV"|"NONE">, "confidence": <0-1>}'
)
_JSON = re.compile(r"\{[^{}]*\"choice\"[^{}]*\}", re.S)
_FENCE = re.compile(r"```(?:json)?\s*|\s*```")
_CHOICE_ONLY = re.compile(r"\"choice\"\s*:\s*(\d+|\"OOV\"|\"NONE\")", re.I)
_CONF_ONLY = re.compile(r"\"confidence\"\s*:\s*([0-9.]+)")


def _parse_choice(txt: str) -> dict | None:
    """Tolerant extraction: full JSON first, then a truncated fragment."""
    s = _FENCE.sub("", txt or "").strip()
    m = _JSON.search(s)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    m = _CHOICE_ONLY.search(s)
    if not m:
        return None
    raw = m.group(1).strip('"')
    conf = _CONF_ONLY.search(s)
    return {
        "choice": raw if raw in ("OOV", "NONE") else int(raw),
        "confidence": float(conf.group(1)) if conf else 0.5,
    }


_URLS = [
    u.strip() for u in os.environ.get("PHASE2_VLLM_URLS", "").split(",") if u.strip()
]
_URL_CYCLE = itertools.count()


def _next_url() -> str:
    """Select the next replica in round-robin order.
    The counter increment is atomic under the GIL, so no lock is required.
    """
    urls = _URLS or [VLLM_URL]
    return urls[next(_URL_CYCLE) % len(urls)]


#: Fields that ask the server for a reasoning pass. They are extensions, not
#: part of the OpenAI-compatible schema, and a server that does not implement
#: them answers 400 for the whole request.
_THINKING_FIELDS = ("chat_template_kwargs", "thinking_token_budget")


def _post(body: dict, timeout: int = 0, retries: int = 3) -> tuple:
    if not timeout:
        timeout = max(300, int(body.get("max_tokens", MAX_TOKENS) / 8) + 180)
    data = json.dumps(body).encode()
    last = None
    dropped_thinking = False
    for attempt in range(retries):
        url = _next_url().rstrip("/") + "/chat/completions"
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
            ch = d["choices"][0]
            msg = ch["message"]
            content = (msg.get("content") or "").strip()
            if THINK and not content:
                content = (msg.get("reasoning_content") or "").strip()
            return content, ch.get("finish_reason")
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code not in (408, 429):
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                except Exception:
                    pass
                # A rejected reasoning extension must not cost the whole
                # curation pass: retry once as a plain request, and say so,
                # because the run is then answering without the think budget
                # it was configured for.
                if not dropped_thinking and any(k in body for k in _THINKING_FIELDS):
                    for k in _THINKING_FIELDS:
                        body.pop(k, None)
                    data = json.dumps(body).encode()
                    dropped_thinking = True
                    print(
                        f"[phase2_llm] server rejected the reasoning extension "
                        f"({e.code}); retrying without it. {detail}",
                        flush=True,
                    )
                    continue
                raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e
            last = e
        except Exception as e:
            last = e
        time.sleep(min(30.0, 2.0 * (attempt + 1)))
    raise RuntimeError(f"vLLM unreachable after {retries} tries: {last!r}")


def normalize_one(label: str, col: str, cands: list[Candidate]) -> dict:
    """Constrained pick. Returns {target,id,source,confidence}."""
    if not cands:
        return {"target": None, "id": "", "source": "no-candidates", "confidence": 0.0}
    lines = []
    for i, c in enumerate(cands):
        src = "Cellosaurus" if c.kind == "cellosaurus" else "MeSH"
        lines.append(f"[{i}] {c.name} — {src} {c.mesh_id}")
    user = f'Field: {col}\nLabel: "{label}"\n\nCandidates:\n' + "\n".join(lines)
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "seed": 0,
        "max_tokens": MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "candidate_pick",
                "schema": {
                    "type": "object",
                    "properties": {
                        "choice": {
                            "anyOf": [
                                {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": max(len(cands) - 1, 0),
                                },
                                {"type": "string", "enum": ["OOV", "NONE"]},
                            ]
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["choice", "confidence"],
                    "additionalProperties": False,
                },
            },
        },
    }
    if THINK:
        body["chat_template_kwargs"] = {"enable_thinking": True}
        if THINK_BUDGET:
            body["thinking_token_budget"] = THINK_BUDGET
    budget = MAX_TOKENS
    txt = ""
    fin = None
    for _attempt in range(3):
        body["max_tokens"] = budget
        txt, fin = _post(body)
        obj = _parse_choice(txt)
        if obj is not None:
            break
        if fin != "length" or budget >= TOKEN_CEILING:
            break
        budget = min(budget * 2, TOKEN_CEILING)
    else:
        obj = None
    if obj is None:
        return {
            "target": None,
            "id": "",
            "source": "truncated" if fin == "length" else "unparseable",
            "confidence": 0.0,
            "raw": txt[:200],
        }
    choice = obj.get("choice")
    conf = float(obj.get("confidence") or 0.0)
    if choice == "NONE":
        return {"target": NS, "id": "", "source": "llm-none", "confidence": conf}
    if choice == "OOV" or not isinstance(choice, int):
        return {"target": None, "id": "", "source": "llm-oov", "confidence": conf}
    if not (0 <= choice < len(cands)):
        return {"target": None, "id": "", "source": "llm-badindex", "confidence": conf}
    c = cands[choice]
    return {
        "target": c.name,
        "id": c.mesh_id,
        "source": "llm-" + ("cellosaurus" if c.kind == "cellosaurus" else "mesh"),
        "confidence": conf,
    }


_SYSTEM_SHORT = (
    "You determine what a short label denotes in one specific study.\n"
    "You are given a field, a short label taken from a sample in that study, "
    "and text belonging to that sample and study.\n"
    "\n"
    "- The label may be an abbreviation, an acronym, a code, or a complete "
    "name. Decide which it is from the text, not from the letters themselves.\n"
    "- Studies define the short forms they use. When the text states what the "
    "label stands for, that is the answer.\n"
    "- When the text does not state it outright but names the concept the "
    "label refers to, answer with that name as the text writes it.\n"
    "- The same letters denote different concepts in different studies. Use "
    "only the text you are given, never a meaning those letters carry "
    "elsewhere.\n"
    "- If the text does not determine what the label denotes, answer with an "
    "empty string. A label left unresolved is preferable to one resolved from "
    "outside the text.\n"
    "- Answer with the concept as a phrase, not a definition or a sentence.\n"
    "\n"
    'Answer with JSON only: {"expansion": "<phrase, or an empty string if '
    'the text does not determine it>", "confidence": <0-1>}'
)


def embed_values(retr: "Retriever", values: list[str]):
    """Vectors for the labels no vocabulary matched.

    Retrieval only decides who is worth comparing. Which labels name one
    concept is decided by the model, because grouping by similarity makes a
    label's fate depend on which other label happened to be processed first,
    and on arithmetic differences far smaller than any difference in meaning.
    """
    if not values:
        return None
    return retr._encode(values).astype(np.float32)


_SYSTEM_CONCEPT = (
    "You decide whether a label names a concept that has already been recorded.\n"
    "You are given a field, one LABEL, and a numbered list of concepts already "
    "recorded for that field. Name the concept the label denotes, or say it is "
    "a new one.\n"
    "\n"
    "- The label names a listed concept when the two denote the same thing and "
    "differ only in how it is written: capitalisation, punctuation, spacing, "
    "word order, singular against plural, or an extra word that adds no "
    "information about what is named.\n"
    "- A label naming the same kind of thing but adding a qualifier that "
    "narrows it, identifies a particular version of it, or states a specific "
    "agent, material, construct, dose or measurement, is a DIFFERENT concept, "
    "even where the rest of the wording is identical.\n"
    "- Judge only from the wording. Do not match a label to a concept because "
    "the two are related, belong to one category, or often occur together.\n"
    "- Uncertain means new. Recording one more concept is always safe; merging "
    "two distinct concepts destroys a distinction the corpus records.\n"
    "\n"
    "When the label names a listed concept, also give the wording that should "
    "name it from now on: the label's own, or the concept's current name, "
    "whichever is clearer and more complete. Take it verbatim from one of the "
    "two; never invent wording.\n"
    "\n"
    'Answer with JSON only: {"concept": <index or null>, "name": "<wording>"}'
)
_CONCEPT_SCHEMA = {
    "type": "object",
    "properties": {
        "concept": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
        "name": {"type": "string"},
    },
    "required": ["concept", "name"],
    "additionalProperties": False,
}


def assign_concept(label: str, col: str, concepts: list[str]) -> dict:
    """Which recorded concept this label names, if any.

    Returns {"concept": index or None, "name": wording}. Anything the model
    cannot answer cleanly returns a new concept under the label's own wording,
    so a failure records one concept too many rather than fusing two.
    """
    fresh = {"concept": None, "name": label, "source": "new"}
    if not concepts:
        return fresh
    lines = [f"[{i}] {c}" for i, c in enumerate(concepts)]
    user = (
        f'Field: {col}\nLabel: "{label}"\n\nConcepts already recorded:\n'
        + "\n".join(lines)
    )
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_CONCEPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "seed": 0,
        "max_tokens": MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "concept_choice", "schema": _CONCEPT_SCHEMA},
        },
    }
    if THINK:
        body["chat_template_kwargs"] = {"enable_thinking": True}
        if THINK_BUDGET:
            body["thinking_token_budget"] = THINK_BUDGET
    try:
        txt, _fin = _post(body)
    except Exception:
        return fresh
    obj = None
    try:
        obj = json.loads(_FENCE.sub("", txt or "").strip())
    except Exception:
        m = re.search(r"\{.*\}", txt or "", re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = None
    if not isinstance(obj, dict):
        return fresh
    c = obj.get("concept")
    if isinstance(c, bool) or not isinstance(c, int) or not (0 <= c < len(concepts)):
        return fresh
    name = obj.get("name")
    if not isinstance(name, str) or name.strip() not in (label, concepts[c]):
        name = concepts[c]
    return {"concept": c, "name": name.strip(), "source": "assigned"}


def expand_short(label: str, col: str, context: str) -> dict:
    """What does this short label denote in this study? Returns {expansion,...}.
    A short form is not resolvable from its own characters, so it is resolved
    against the words of the study that used it. `expansion` is None whenever
    the text does not settle the question -- an unresolved label is passed
    through unchanged rather than replaced by a guess.
    """
    if not context:
        return {"expansion": None, "source": "no-context", "confidence": 0.0}
    user = (
        f'Field: {col}\nLabel: "{label}"\n\n'
        f"Text from this sample and study:\n{context}"
    )
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_SHORT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "seed": 0,
        "max_tokens": MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "short_expansion",
                "schema": {
                    "type": "object",
                    "properties": {
                        "expansion": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["expansion", "confidence"],
                    "additionalProperties": False,
                },
            },
        },
    }
    if THINK:
        body["chat_template_kwargs"] = {"enable_thinking": True}
        if THINK_BUDGET:
            body["thinking_token_budget"] = THINK_BUDGET
    budget, txt, fin = MAX_TOKENS, "", None
    for _attempt in range(3):
        body["max_tokens"] = budget
        txt, fin = _post(body)
        obj = _parse_choice(txt)
        if obj is not None:
            break
        if fin != "length" or budget >= TOKEN_CEILING:
            break
        budget = min(budget * 2, TOKEN_CEILING)
    else:
        obj = None
    if obj is None:
        m = re.search(r'"expansion"\s*:\s*"([^"]*)"', txt or "")
        if m:
            obj = {"expansion": m.group(1), "confidence": 0.5}
        else:
            return {
                "expansion": None,
                "confidence": 0.0,
                "source": "truncated" if fin == "length" else "unparseable",
            }
    exp = obj.get("expansion")
    exp = exp.strip() if isinstance(exp, str) and exp.strip() else None
    if exp and exp.strip().lower() == label.strip().lower():
        exp = None
    return {
        "expansion": exp,
        "confidence": float(obj.get("confidence") or 0.0),
        "source": "llm-short" if exp else "llm-short-unresolved",
    }


def mint_oov(label: str, col: str, counter: dict) -> dict:
    """Local identifier in our own namespace. Never a fake MeSH id -- a fabricated
    D-number would break every downstream join and make the corpus
    non-interoperable.

    The identifier is derived from the concept's own wording rather than from a
    running count, so it does not depend on how many concepts were minted
    before it. A counter gives the same number to different concepts whenever
    the stage runs more than once -- across the two minting passes, or across
    shards -- and a downstream join on the identifier then silently fuses
    concepts that were never related.
    """
    prefix = {"Tissue": "T", "Condition": "C", "Treatment": "X"}.get(col, "X")
    key = f"{col}\x00{' '.join(str(label or '').lower().split())}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10].upper()
    counter[col] = counter.get(col, 0) + 1
    return {
        "target": label,
        "id": f"OOV-{prefix}-{h}",
        "source": "oov",
        "confidence": 1.0,
    }


__all__ = [
    "Retriever",
    "Candidate",
    "normalize_one",
    "expand_short",
    "mint_oov",
    "assign_concept",
    "embed_values",
    "VLLM_URL",
    "MODEL",
    "THINK",
    "TOP_K",
]
