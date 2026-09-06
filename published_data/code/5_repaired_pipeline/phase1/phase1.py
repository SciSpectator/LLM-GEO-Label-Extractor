"""Phase 1 verbatim-extraction runtime.

Loads the per-label prompts previously optimised offline by
``compile_phase1.py`` from ``compiled/*.json`` and renders them into
chat messages in the ``[[ ## field ## ]]`` layout. The LM itself is
called with stdlib HTTP.

Public API:
    * ``NS``                     — sentinel "Not Specified" string
    * ``extract_tissue(raw)``    — per-sample tissue span
    * ``extract_condition(raw)`` — per-sample condition/disease span
    * ``extract_treatment(raw)`` — per-sample treatment span
    * ``extract_sex(raw)``       — per-sample sex/gender span (demographic)
    * ``extract_age(raw)``       — per-sample age span in structured form
                                   (``<unit>: <number>`` or ``age: <descriptor>``)
    * ``Phase1Agent``            — ``.extract(raw)`` → {Tissue, Condition,
                                   Treatment, Sex, Age}

Sex and Age are demographic labels: extracted by Phase 1 only and passed
through Phase 1b / 1c / 2 verbatim (GSE-level context cannot infer
per-subject demographics, and MeSH normalisation does not apply).

Environment variables:
    * ``PHASE1_BACKEND``   — ``ollama`` (default), or any OpenAI-compatible
                             server: ``vllm`` / ``sglang`` / ``openai``
    * ``PHASE1_MODEL default is gemma4-e2b-text:latest`
    * ``OLLAMA_URL``       — default ``http://localhost:11434``
    * ``SGLANG_URL`` / ``VLLM_URL`` / ``OPENAI_BASE_URL``
                           — OpenAI-compatible endpoint; default
                             ``http://localhost:8000/v1`` (30000 for sglang)

Reproducibility: temperature=0, seed=42, num_predict=-1.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Tuple

NS = "Not Specified"


_BACKEND = os.environ.get("PHASE1_BACKEND", "ollama").lower()
_MODEL = os.environ.get("PHASE1_MODEL", "gemma4-e2b-text:latest")


if _BACKEND in ("sglang", "vllm", "openai"):
    _OPENAI_URL = (
        os.environ.get("SGLANG_URL")
        or os.environ.get("VLLM_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or (
            "http://localhost:30000/v1"
            if _BACKEND == "sglang"
            else "http://localhost:8000/v1"
        )
    )
    _CHAT_URL = _OPENAI_URL.rstrip("/") + "/chat/completions"
    _IS_OPENAI = True
else:
    _OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    _CHAT_URL = _OLLAMA_URL.rstrip("/") + "/api/chat"
    _IS_OPENAI = False


_Field = Tuple[str, str, str]

_SIG_TISSUE: Dict = {
    "artifact": "extract_tissue.json",
    "inputs": [
        ("title", "str", "Sample title."),
        ("source", "str", "Sample source_name field."),
        (
            "characteristics",
            "str",
            "Sample characteristics field — usually key:value pairs separated by ';' or '|'.",
        ),
        ("treatment_protocol", "str", "Sample treatment/lab protocol text."),
        ("description", "str", "Sample description."),
    ],
    "outputs": [
        ("tissue", "str", "Verbatim tissue/organ/cell span, or 'Not Specified'."),
    ],
}

_SIG_CONDITION: Dict = {
    "artifact": "extract_condition.json",
    "inputs": [
        ("title", "str", "Sample title."),
        ("source", "str", "Sample source_name field."),
        (
            "characteristics",
            "str",
            "Sample characteristics field — usually key:value pairs.",
        ),
        ("treatment_protocol", "str", "Sample treatment/lab protocol text."),
        ("description", "str", "Sample description."),
    ],
    "outputs": [
        (
            "condition",
            "str",
            "Verbatim condition / disease / phenotype, or 'Not Specified'.",
        ),
    ],
}

_SIG_TREATMENT: Dict = {
    "artifact": "extract_treatment.json",
    "inputs": [
        ("title", "str", "Sample title."),
        ("source", "str", "Sample source_name field."),
        (
            "characteristics",
            "str",
            "Sample characteristics field — usually key:value pairs.",
        ),
        (
            "treatment_protocol",
            "str",
            "Sample treatment/lab protocol text — may contain lab handling (not a treatment) or the actual experimental treatment. Read carefully.",
        ),
        ("description", "str", "Sample description."),
    ],
    "outputs": [
        (
            "treatment",
            "str",
            "Verbatim treatment / drug / intervention, or 'Not Specified'.",
        ),
    ],
}

_SIG_SEX: Dict = {
    "artifact": "extract_sex.json",
    "inputs": [
        ("title", "str", "Sample title."),
        ("source", "str", "Sample source_name field."),
        (
            "characteristics",
            "str",
            "Sample characteristics field — usually key:value pairs.",
        ),
        ("treatment_protocol", "str", "Sample treatment/lab protocol text."),
        ("description", "str", "Sample description."),
    ],
    "outputs": [
        (
            "sex",
            "str",
            "Verbatim sex/gender span (e.g. 'male', 'female', 'M', 'F'), or 'Not Specified'.",
        ),
    ],
}

_SIG_AGE: Dict = {
    "artifact": "extract_age.json",
    "inputs": [
        ("title", "str", "Sample title."),
        ("source", "str", "Sample source_name field."),
        (
            "characteristics",
            "str",
            "Sample characteristics field — usually key:value pairs.",
        ),
        ("treatment_protocol", "str", "Sample treatment/lab protocol text."),
        ("description", "str", "Sample description."),
    ],
    "outputs": [
        (
            "age",
            "str",
            "Structured age span: '<unit>: <number>' with unit ∈ {years, months, weeks, days}, OR 'age: <descriptor>' for qualitative life-stage tokens, OR 'Not Specified'.",
        ),
    ],
}

_SIG_RESOLVE: Dict = {
    "artifact": "resolve_coded_value.json",
    "inputs": [
        (
            "field",
            "str",
            "Which label this is — one of 'Tissue', 'Condition', 'Treatment'. Helps disambiguate which legend slot applies.",
        ),
        (
            "raw_value",
            "str",
            "The verbatim value just extracted by the Phase 1 per-label signature.",
        ),
        (
            "metadata_blob",
            "str",
            "All sample metadata concatenated (title, source, characteristics, treatment_protocol, description). Search HERE for a literal legend.",
        ),
    ],
    "outputs": [
        (
            "resolved",
            "str",
            "raw_value with coded tokens substituted by legend-text meanings, OR raw_value byte-identical when no legend applies, OR 'Not Specified' when resolution collapses to nothing.",
        ),
    ],
}


_FALLBACK_TISSUE = """Extract the single best TISSUE / ORGAN / CELL-TYPE / CELL-LINE span for
THIS sample, copied VERBATIM from the metadata text.

Hierarchy (walk in order, stop at the first non-empty source):
  1. `cell line:` — the value IS the span.
  2. `cell type:` — only consulted when `cell line:` is absent.
  3. `tissue:` — only consulted when both cell_line and cell_type are absent.
  4. `organ:` / `anatomical site:` / `biopsy site:` — only when 1-3 absent.
  5. `source_name` — only when 1-4 absent. Extract verbatim.
  6. title / description — last resort.

Compound "<Organ> Cancer/Tumor/Carcinoma/Neoplasm": return the ORGAN noun alone
for spans with an EXPLICIT organ noun. Keep abbreviated cancers (OSCC, HCC,
TNBC, ...) and cell-line IDs whole.

If no tissue / organ / cell-type / cell-line is named, output: Not Specified.
"""

_FALLBACK_CONDITION = """Extract the CONDITION(S) of THIS SAMPLE — disease(s), pathological state,
OR explicit healthy / control / normal sample state — as it appears in the
metadata.

WHAT COUNTS (three equal-priority categories):
 (1) Disease names, phenotypes, stage / grade / severity markers.
 (2) Explicit healthy / control / normal sample state. When THIS sample's own
     metadata literally marks it as control / normal / healthy / non-disease /
     non-tumor / unaffected / uninflamed, EMIT that state verbatim. Do NOT
     emit "Not Specified" when a control / normal / healthy marker is
     documented for this sample.
 (3) Pathological phenotype markers without a named disease ("tumor", "lesion",
     "metastasis").

THIS-SAMPLE scoping: extract only the condition(s) or sample-state THIS sample
HAS. The GSE topic alone is not evidence — if the GSE studies a disease but
THIS sample's text says "control" / "healthy donor" / "non-tumor", THIS
sample's Condition is the control state, NOT the GSE disease.

Strip Tissue/cell-line descriptors around embedded disease names (e.g.
"glioblastoma stem-like cell line" → "glioblastoma").

Multiple conditions for one sample are joined with "; " in the order they
appear. Return "Not Specified" only when NO disease, phenotype, stage, OR
explicit healthy / control / normal marker appears anywhere in the metadata
for this sample.

CELL-LINE IDENTIFIER IS NEVER A CONDITION (critical): a bare cell-line code
(SKOV3, HCC827/"HCC 827", MCF-7, HL60, T47D, BCBL1, A549) is a Tissue value.
When it co-occurs with its disease ("SKOV3 Human Ovarian Cancer cell line"),
the Condition is the DISEASE phrase ("Ovarian Cancer"), never the code. If only
the code is present with no stated disease, output Not Specified.
"""

_FALLBACK_TREATMENT = """Extract the treatment(s) applicable to THIS sample — the drug(s),
compound(s), genetic perturbation(s), exposure(s), or clinical/experimental
procedure(s) applied to this particular sample.

Keep intervention names verbatim including dose/concentration tokens when
attached. Multi-treatment → join with "; ".

WHAT DECIDES THIS FIELD IS WHETHER SOMETHING WAS ADMINISTERED TO THE SAMPLE,
not whether the sample served as the experiment's comparison arm. A sample's
role as a control says nothing about whether it received something.
  - A substance or construct that WAS given is a Treatment even when its purpose
    is to be the baseline: an inert carrier or solvent given in place of an
    active agent, a placebo, an inactive or non-targeting counterpart of an
    active construct, an empty or payload-free delivery vector. Return it, e.g.
    "DMSO", "vehicle", "Placebo", "shRNA_Ctrl", "empty vector", "siControl".
  - When the designation names NOTHING that was given, return "Not Specified":
    an untreated, naive, parental or sham condition, a bare comparison label
    with no substance in it ("control", "negative control"), a dose of zero
    ("0Gy", "0nM dasatinib"), or an explicitly negated exposure ("no drug",
    "never statin user").
Decide this by what the words mean, not by matching any fixed vocabulary.

NO WORLD-KNOWLEDGE / CELL-LINE DRUG INFERENCE: the intervention must be
literally named in THIS sample's own text; never infer a drug from the cell
line's or disease's known clinical use.

SHARED-PROTOCOL CONTROL-ARM GUARD (control-leak): a treatment_protocol often
describes the WHOLE experiment and is copied onto control samples. When THIS
sample's own title/source/characteristics mark it as a control / vehicle /
untreated / mock / vector / non-targeting / scrambled / parental / wild-type /
naive / baseline / "NT" arm, do NOT assign the treated arms' active drug(s);
emit only a literally-named control substring, else "Not Specified".

TREATMENT — UNIVERSAL DEFINITION (overrides any narrower phrasing above):
A Treatment is an in-vivo or in-vitro PERTURBATION applied to the
biological sample's source organism / tissue / cell line BEFORE the
molecular assay was run — drugs and their doses; genetic perturbations
(shRNA, siRNA, CRISPR, KO, OE, inducible expression, transgene);
irradiation; environmental exposures (hypoxia, heat shock, UV, smoke);
or clinical procedures. Text that describes the MOLECULAR ASSAY ITSELF
— chemical conversion of nucleic acids, hybridisation, library
construction, scan / sequencing protocol, kit names, reagent names,
instrument names — is NOT a Treatment, regardless of whether it
appears in a field literally named `treatment_protocol`. When the only
treatment-shaped text in the sample's metadata is assay-protocol text,
output exactly: Not Specified.
"""

_FALLBACK_SEX = """Extract the SEX / GENDER of THIS sample's subject, using only this sample's own metadata.

Search in priority order, stopping at the first field that carries a sex value:
any characteristics field whose name is or contains "sex" or "gender"
(including donor / subject / patient / animal variants), then the title,
source, or description if a sex token appears there unambiguously for this
sample.

Emit the sex in plain meaning: male, female, or — for an explicitly pooled
mixed-sex sample — the components joined with "; " in the order they appear.

LEGEND DECODING (structural, never world knowledge): when the field NAME itself
embeds a code-to-meaning key — the name pairs each possible value with a word —
the stored value is a CODE. Read the key left to right, match the value to its
code, and emit the paired word, not the code. The key may use -, =, :, /, or →
as the code/word separator and may sit inside ( ), [ ], or after --.

CODED VALUE WITHOUT A LEGEND: if the sex value is a bare numeric code
(e.g. 0, 1, 2) and no code-to-meaning key is embedded in THIS sample's own
field name, the mapping is unknown - emit "Not Specified". Never infer which
number means which sex from world knowledge or dataset convention.

NEVER INFER SEX FROM BIOLOGY OR WORLD KNOWLEDGE: sex must come from an explicit
sex/gender statement (or an in-sample legend), and must NOT be deduced from a
disease, organ, tissue, reproductive anatomy, hormone, or physiological state
that merely correlates with a sex. A sex-linked biological cue is NOT a sex
statement. If the only signal is such a correlate, emit "Not Specified".

Emit "Not Specified" only when the metadata gives no sex, or gives only a
null / unknown placeholder (unknown, N/A, NA, none, not collected, not
reported, -).
"""

_FALLBACK_AGE = """Extract the AGE of THIS sample's own subject from this sample's own metadata.
Use the subject's OWN age only -- never another individual's age in the same
record (e.g. a maternal / paternal / donor age carried in a separate field).

OUTPUT -- use the FIRST form that fits, and format it EXACTLY:
  A. structured  ->  <unit>: <number>
     unit is one of years, months, weeks, days; number is digits (decimals
     allowed; a range is N-M). Unit comes FIRST, then a colon, then the number
     (years: 37, months: 10, weeks: 22, years: 6-8). Use whenever the age is a
     clean number placeable on one of the four units.
  B. verbatim    ->  age: <expression>
     the source age text, lowercased and whitespace-collapsed. Use for ANY age
     form A cannot represent.

Find the value in priority order, stopping at the FIRST field that gives THIS
subject's age: a characteristics field whose name is or contains "age" (incl.
gestational age, postnatal day, developmental / life stage) but is NOT another
person's age; then title / source / description.

FORM A unit, first rule that applies:
  1. a unit token on the number (year/yr/y, month/mo, week/wk, day/d);
  2. otherwise a unit named in the field itself;
  3. otherwise a bare number under an age field is the subject's age in years.
Convert spelled-out numbers to digits. Postnatal "day N" / "P N" / "PND N" ->
days: N. Approximate markers (~, about, approx, average, mean, median) are
dropped -- keep the number in form A.

FORM B (age: <expression>), kept verbatim -- never Not Specified -- for age
information form A cannot hold:
  - a comparator bound (<, >, <=, >=, under, over, younger/older than, up to,
    at least): e.g. age: <1.5 years, age: >50 years;
  - gestational / embryonic / developmental shorthand (22 weeks gestation,
    GW22, E14.5, blastocyst, larval L3);
  - a qualitative life stage with no number+unit (newborn, neonate, infant,
    juvenile, adult, elderly, fetal);
  - any other non-standard age phrasing.

NOT an age -> "Not Specified": a lone code under an "age group / age category /
age class" field with no stated age, or a coded token with no legend in this
sample.

Several distinct ages for THIS subject -> join with "; " in source order. When
a number and a descriptor both appear for the subject, keep the number. Emit
"Not Specified" ONLY when THIS subject has no age at all, or only a null /
unknown placeholder (unknown, N/A, NA, none, not collected, not reported, -).
"""

_FALLBACK_RESOLVE = """Decode any coded token inside the extracted label value by consulting a
legend present in the same sample's metadata. If no legend applies, return
raw_value BYTE-IDENTICAL.

Legend shapes you must recognise (in order):

  1. EXPLICIT DEFINITION elsewhere in metadata: when a token in
     raw_value is defined in the sample text (e.g. a key/legend like
     "X = meaning"), substitute the definition.

  2. SELF-LEGENDING FIELD NAME — the field name itself names the
     condition / phenotype, and its value is a polarity axis. The
     field name acts AS the legend.
     Apply when raw_value has the form `<field>: <axis_token>`.
     The axis_token decides the polarity:
       - affirmative axis (yes, Y, 1, true, positive, pos, present,
         affected, case) → output the condition named in the field
         itself (drop trailing suffixes such as "diagnosis", ".status",
         "_yes_no", "_yn", "_status"; capitalise normally).
       - negative axis (no, N, 0, false, negative, neg, absent,
         unaffected, control) → output "Not Specified".
     For composite raw_value with multiple `<field>: <axis_token>`
     components separated by "; ", resolve each component independently
     under the same rule and join the surviving (non-NS) outputs with
     "; ".
     This is NOT world knowledge: the field name is literally present
     in the sample's metadata, and the rule is purely structural —
     "the field name names the condition when the value is on the
     affirmative axis."

  3. IN-FIELD PARENTHETICAL — code and name side by side, e.g.
       "RRMS (Relapsing Remitting Multiple Sclerosis)"
     keep the full clinical name.

If resolution collapses to nothing (e.g. all components on the negative
axis), return "Not Specified". Output BYTE-IDENTICAL only when none of
the three legend shapes apply.
"""


_ARTIFACTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compiled")


def _load_artifact(spec: Dict, fallback: str) -> Dict:
    """Load ``{instructions, demos}`` from a MIPROv2 artifact; fall back to
    the Signature docstring if the file is missing or malformed."""
    path = os.path.join(_ARTIFACTS_DIR, spec["artifact"])
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            ins = (data.get("signature") or {}).get("instructions") or fallback
            demos = data.get("demos") or []
            return {"instructions": ins, "demos": demos}
        except Exception as e:
            print(
                f"[phase1_runtime] could not load {spec['artifact']}: "
                f"{e!r} — using fallback prompt"
            )
    return {"instructions": fallback, "demos": []}


_PROMPT_TISSUE = _load_artifact(_SIG_TISSUE, _FALLBACK_TISSUE)
_PROMPT_CONDITION = _load_artifact(_SIG_CONDITION, _FALLBACK_CONDITION)
_PROMPT_TREATMENT = _load_artifact(_SIG_TREATMENT, _FALLBACK_TREATMENT)
_PROMPT_SEX = _load_artifact(_SIG_SEX, _FALLBACK_SEX)
_PROMPT_AGE = _load_artifact(_SIG_AGE, _FALLBACK_AGE)
_PROMPT_RESOLVE = _load_artifact(_SIG_RESOLVE, _FALLBACK_RESOLVE)


def _render_system(spec: Dict, instructions: str) -> str:
    lines: List[str] = ["Your input fields are:"]
    for i, (n, t, d) in enumerate(spec["inputs"], 1):
        lines.append(f"{i}. `{n}` ({t}): {d}")
    lines.append("Your output fields are:")
    for i, (n, t, d) in enumerate(spec["outputs"], 1):
        lines.append(f"{i}. `{n}` ({t}): {d}")
    lines.append(
        "All interactions will be structured in the following way, "
        "with the appropriate values filled in."
    )
    lines.append("")
    for n, _, _ in spec["inputs"]:
        lines.append(f"[[ ## {n} ## ]]")
        lines.append("{" + n + "}")
        lines.append("")
    for n, _, _ in spec["outputs"]:
        lines.append(f"[[ ## {n} ## ]]")
        lines.append("{" + n + "}")
        lines.append("")
    lines.append("[[ ## completed ## ]]")

    indented = "\n".join("        " + L for L in instructions.splitlines())
    lines.append(f"In adhering to this structure, your objective is: \n" f"{indented}")
    return "\n".join(lines)


def _render_user(spec: Dict, values: Dict[str, str]) -> str:
    parts: List[str] = []
    for n, _, _ in spec["inputs"]:
        parts.append(f"[[ ## {n} ## ]]")
        parts.append(str(values.get(n, "") or ""))
        parts.append("")
    first_out = spec["outputs"][0][0]
    parts.append(
        f"Respond with the corresponding output fields, starting with the "
        f"field `[[ ## {first_out} ## ]]`, and then ending with the marker "
        f"for `[[ ## completed ## ]]`."
    )
    return "\n".join(parts)


def _render_demo_pair(spec: Dict, demo: Dict) -> List[Dict[str, str]]:
    """Render one few-shot demo as a user/assistant message pair in
    ChatAdapter format."""
    user = _render_user(spec, demo)
    asst_parts: List[str] = []
    for n, _, _ in spec["outputs"]:
        asst_parts.append(f"[[ ## {n} ## ]]")
        asst_parts.append(str(demo.get(n, "") or ""))
        asst_parts.append("")
    asst_parts.append("[[ ## completed ## ]]")
    return [
        {"role": "user", "content": user},
        {"role": "assistant", "content": "\n".join(asst_parts)},
    ]


def _build_messages(
    spec: Dict, prompt: Dict, values: Dict[str, str]
) -> List[Dict[str, str]]:
    msgs: List[Dict[str, str]] = [
        {"role": "system", "content": _render_system(spec, prompt["instructions"])},
    ]
    for demo in prompt.get("demos", []):
        msgs.extend(_render_demo_pair(spec, demo))
    msgs.append({"role": "user", "content": _render_user(spec, values)})
    return msgs


try:
    import requests as _requests_mod

    _SESSION = _requests_mod.Session()
    _SESSION.mount(
        "http://",
        _requests_mod.adapters.HTTPAdapter(
            pool_connections=256, pool_maxsize=256, max_retries=0
        ),
    )
    _SESSION.mount(
        "https://",
        _requests_mod.adapters.HTTPAdapter(
            pool_connections=256, pool_maxsize=256, max_retries=0
        ),
    )
except Exception:
    _requests_mod = None
    _SESSION = None


def _http_post_json(url: str, body: Dict, timeout: int = 600) -> Dict:
    if _SESSION is not None:
        try:
            r = _SESSION.post(
                url,
                json=body,
                timeout=timeout,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "keep-alive",
                },
            )
        except _requests_mod.exceptions.RequestException as e:

            raise urllib.error.URLError(str(e))
        if r.status_code >= 400:
            raise urllib.error.HTTPError(
                url, r.status_code, r.reason, getattr(r, "headers", None), None
            )
        return r.json()

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _call_lm(messages: List[Dict[str, str]]) -> str:
    if _IS_OPENAI:
        body = {
            "model": _MODEL,
            "messages": messages,
            "temperature": 0.0,
            "seed": 42,
            "max_tokens": int(os.environ.get("PHASE1_MAX_TOKENS", "256")),
        }
        resp = _http_post_json(_CHAT_URL, body)
        return resp["choices"][0]["message"]["content"]

    _THINK = os.environ.get("PHASE1_THINK", "false").lower() in ("1", "true", "yes")
    body = {
        "model": _MODEL,
        "messages": messages,
        "stream": False,
        "think": _THINK,
        "options": {
            "temperature": 0.0,
            "seed": 42,
            "num_ctx": int(os.environ.get("PHASE1_NUM_CTX", "8192")),
            "num_predict": 1024 if _THINK else 256,
        },
    }
    resp = _http_post_json(_CHAT_URL, body)
    msg = resp.get("message", {})

    return (msg.get("content") or "") + (
        "\n" + msg.get("thinking", "") if _THINK else ""
    )


_FIELD_RX = re.compile(r"\[\[ ## (?P<name>[^#]+?) ## \]\]")


_MARKER_RESIDUE = re.compile(r"\s*\[\[\s*##.*$", re.S)


def _strip_markers(s: str) -> str:
    """Strip trailing DSPy field/completion marker residue that a
    malformed marker (e.g. '[[ ## completed ]]' missing the 2nd ##)
    left riding along in the value."""
    return _MARKER_RESIDUE.sub("", s or "").strip()


def _parse_output(text: str, out_name: str) -> str:
    """Return the content between ``[[ ## out_name ## ]]`` and the next
    marker (another field or ``[[ ## completed ## ]]``). If no marker is
    present, treat the whole text as the output value."""
    if not text:
        return ""

    positions = [
        (m.start(), m.end(), m.group("name").strip()) for m in _FIELD_RX.finditer(text)
    ]
    if not positions:
        return text.strip()
    for i, (_, end, name) in enumerate(positions):
        if name == out_name:
            next_start = positions[i + 1][0] if i + 1 < len(positions) else len(text)
            return text[end:next_start].strip()

    return text[: positions[0][0]].strip()


def _normalize_ns(val) -> str:
    if not val:
        return NS
    v = str(val).strip().strip('"').strip("'")
    if not v or v.lower() in ("not specified", "none", "null", "n/a", "na"):
        return NS
    return v


def _args_from_raw(raw: Dict) -> Dict[str, str]:
    return {
        "title": str(raw.get("gsm_title", "") or "").strip(),
        "source": str(raw.get("source_name", "") or "").strip(),
        "characteristics": str(raw.get("characteristics", "") or "")
        .replace("\t", " ")
        .strip(),
        "treatment_protocol": str(raw.get("treatment_protocol", "") or "")
        .replace("\t", " ")
        .strip(),
        "description": str(raw.get("description", "") or "").replace("\t", " ").strip(),
    }


def _metadata_blob(args: Dict[str, str]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in args.items() if v)


_VB_WORD = re.compile(r"[a-z0-9]+")


_VB_STOP = {
    "of",
    "the",
    "and",
    "in",
    "with",
    "a",
    "an",
    "for",
    "to",
    "or",
    "on",
    "by",
    "from",
    "at",
}


def _vb_squash(s: str) -> str:
    """Lowercase and strip all non-alphanumerics so span matching is robust to
    spacing/punctuation drift ('5 mg/kg' vs '5mg/kg', 'HER-2' vs 'HER2')."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _verbatim_enforce(value: str, args: Dict[str, str]) -> str:
    """Pure-extraction guard. Phase 1 must COPY spans from the metadata, never
    generate. A ';'-separated part is kept only if it is either (a) a spacing/
    punctuation-insensitive substring of the metadata, or (b) a reordering whose
    EVERY content word (non-stopword) appears in the metadata. A part containing
    ANY word absent from the source is a hallucination and is dropped.

    This fixes two failure modes of the naive >=60% bag-of-words rule:
      * it no longer DROPS faithful dose spans like '5 mg/kg' that only differ
        from the source '5mg/kg' in spacing/punctuation (squash-substring), and
      * it no longer KEEPS a fabricated 3rd word (e.g. an invented 'carcinoma')
        riding along on two grounded words (requires ALL content words present).
    It still stops a 'Left Ventricle Biopsy' sample being labelled with a
    generated list of cardiomyopathy subtypes nowhere in its metadata."""
    if _normalize_ns(value) == NS:
        return value
    blob = _metadata_blob(args).lower()
    blob_1line = re.sub(r"\s+", " ", blob)
    blob_squash = _vb_squash(blob)
    blob_words = set(_VB_WORD.findall(blob))
    kept = []
    for part in str(value).split(";"):
        p = part.strip()
        if not p:
            continue
        pl = re.sub(r"\s+", " ", p.lower())
        if pl in blob_1line or _vb_squash(p) in blob_squash:
            kept.append(p)
            continue
        content = [w for w in _VB_WORD.findall(pl) if w not in _VB_STOP]
        if content and all(w in blob_words for w in content):
            kept.append(p)

    return "; ".join(kept) if kept else NS


def _predict_label(
    spec: Dict, prompt: Dict, out_name: str, values: Dict[str, str]
) -> str:
    messages = _build_messages(spec, prompt, values)

    attempt = 0
    malformed = 0
    while True:
        try:
            raw = _call_lm(messages)
            return _strip_markers(_parse_output(raw, out_name))
        except urllib.error.HTTPError as e:

            if 400 <= e.code < 500 and e.code not in (408, 429):
                print(
                    f"[p1 FATAL] {out_name}: permanent HTTP {e.code}: {e!r}",
                    flush=True,
                )
                raise
            attempt += 1
            if attempt % 20 == 0:
                print(
                    f"[p1 retry] {out_name}: {attempt} transient retries; last={e!r}",
                    flush=True,
                )
            time.sleep(min(60.0, 1.5 * attempt))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:

            attempt += 1
            if attempt % 20 == 0:
                print(
                    f"[p1 retry] {out_name}: {attempt} transport retries; last={e!r}",
                    flush=True,
                )
            time.sleep(min(60.0, 1.5 * attempt))
        except (json.JSONDecodeError, KeyError, IndexError) as e:

            malformed += 1
            if malformed > 5:
                print(
                    f"[p1 FATAL] {out_name}: {malformed} malformed responses; last={e!r}",
                    flush=True,
                )
                raise
            print(
                f"[p1 retry] {out_name}: malformed response #{malformed}; last={e!r}",
                flush=True,
            )
            time.sleep(min(30.0, 2.0 * malformed))


def _resolve_if_coded(field: str, raw_value: str, args: Dict[str, str]) -> str:
    if _normalize_ns(raw_value) == NS:
        return NS
    values = {
        "field": field,
        "raw_value": raw_value,
        "metadata_blob": _metadata_blob(args),
    }
    try:
        out = _predict_label(_SIG_RESOLVE, _PROMPT_RESOLVE, "resolved", values)
    except Exception:
        return raw_value
    return _normalize_ns(out) if out else raw_value


def extract_tissue(raw: Dict) -> str:
    args = _args_from_raw(raw)
    out = _predict_label(_SIG_TISSUE, _PROMPT_TISSUE, "tissue", args)
    initial = _normalize_ns(out)
    return _verbatim_enforce(_resolve_if_coded("Tissue", initial, args), args)


_DENIAL_RX = re.compile(
    r"^[A-Za-z][A-Za-z0-9_.\- ]*\s*[:=]\s*"
    r"(N|No|0|false|negative|absent|unaffected|none|neg)\s*$",
    re.IGNORECASE,
)


def _is_denied_field_value(s: str) -> bool:
    if not s or NS.lower() in s.lower():
        return False
    return bool(_DENIAL_RX.match(s.strip()))


def _condition_cellline_recover(val: str, raw: Dict) -> str:
    """Deterministic cell-line-as-Condition correction: a bare cell-line
    identifier (recognised in Cellosaurus) is a Tissue value, not a disease.
    When the value IS just a recognised cell line, replace it with the disease
    phrase stated in the sample's own raw text, or 'Not Specified' if none. Uses
    the reference vocabulary (not a regex) so disease abbreviations like T2D are
    never mistaken for cell-line codes."""
    if not val or NS.lower() in val.lower():
        return val
    try:
        from cellline_db import CellLineDB
        from phase1b import _recover_disease, _DISEASE_PHRASE
    except Exception:
        return val
    v = val.strip()
    if _DISEASE_PHRASE.search(v):
        return val
    db = CellLineDB.get()
    hit = None
    for cand in (v, re.sub(r"\s+", "", v)):
        m = db.match(cand)
        if m and re.sub(r"\s+", "", m).lower() == re.sub(r"\s+", "", v).lower():
            hit = m
            break
    if not hit:
        return val
    own = " ".join(
        str(raw.get(k, "") or "")
        for k in (
            "title",
            "gsm_title",
            "source",
            "source_name",
            "source_name_ch1",
            "characteristics",
            "characteristics_ch1",
            "description",
        )
    )
    dis = _recover_disease(own)
    return dis if dis else NS


def extract_condition(raw: Dict) -> str:
    args = _args_from_raw(raw)
    out = _predict_label(_SIG_CONDITION, _PROMPT_CONDITION, "condition", args)
    initial = _normalize_ns(out)
    if _is_denied_field_value(initial):
        return NS
    resolved = _verbatim_enforce(_resolve_if_coded("Condition", initial, args), args)
    return _condition_cellline_recover(resolved, raw)


def extract_treatment(raw: Dict) -> str:
    args = _args_from_raw(raw)
    out = _predict_label(_SIG_TREATMENT, _PROMPT_TREATMENT, "treatment", args)
    initial = _normalize_ns(out)
    return _verbatim_enforce(_resolve_if_coded("Treatment", initial, args), args)


_DEFER = object()

_NULLISH = {
    "",
    "na",
    "n/a",
    "none",
    "null",
    "unknown",
    "not collected",
    "not reported",
    "not available",
    "-",
    "--",
    ".",
}


def _kv_pairs(text: str):
    out = []
    for seg in re.split(r"[;\t\n]", text or ""):
        if ":" not in seg:
            continue
        k, v = seg.split(":", 1)
        out.append((k.strip(), v.strip()))
    return out


def _fast_sex(raw: Dict):
    """'male' / 'female', or _DEFER to hand the sample to the reasoning LLM."""
    found = None
    for k, v in _kv_pairs(str(raw.get("characteristics", "") or "")):
        kl = k.lower()
        if "sex" not in kl and "gender" not in kl:
            continue

        if "(" in k or "[" in k or "=" in k:
            return _DEFER
        vl = v.lower().strip().strip(".")
        if vl in _NULLISH:
            continue
        if vl in ("male", "m", "man", "boy"):
            cand = "male"
        elif vl in ("female", "f", "woman", "girl"):
            cand = "female"
        else:

            return _DEFER
        if found is not None and found != cand:
            return _DEFER
        found = cand
    return found if found is not None else _DEFER


_UNIT = {
    "year": "years",
    "years": "years",
    "yr": "years",
    "yrs": "years",
    "y": "years",
    "month": "months",
    "months": "months",
    "mo": "months",
    "mos": "months",
    "mon": "months",
    "week": "weeks",
    "weeks": "weeks",
    "wk": "weeks",
    "wks": "weeks",
    "w": "weeks",
    "day": "days",
    "days": "days",
    "d": "days",
}
_AGE_NUM = re.compile(r"^(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)\s*([A-Za-z]+)?$")
_KEY_UNIT = re.compile(r"\b(years?|yrs?|months?|mos?|mon|weeks?|wks?|days?)\b", re.I)


def _fast_age(raw: Dict):
    """'<unit>: <number>', or _DEFER to hand the sample to the reasoning LLM."""
    hits = []
    for k, v in _kv_pairs(str(raw.get("characteristics", "") or "")):
        kl = k.lower()
        if "age" not in kl:
            continue

        if (
            any(t in kl for t in ("stage", "category", "group", "class", "gestation"))
            or "=" in k
        ):
            return _DEFER
        vl = v.lower().strip().strip(".")
        if vl in _NULLISH:
            continue
        m = _AGE_NUM.match(vl)
        if not m:
            return _DEFER
        num = m.group(1).replace(" ", "")
        tok = (m.group(2) or "").lower()
        if tok:
            if tok not in _UNIT:
                return _DEFER
            unit = _UNIT[tok]
        else:
            km = _KEY_UNIT.search(kl)
            unit = _UNIT[km.group(1).lower()] if km else "years"
        try:
            if unit == "years" and float(num.split("-")[0]) > 120:
                return _DEFER
        except ValueError:
            pass
        hits.append(f"{unit}: {num}")
    if not hits or len(set(hits)) > 1:
        return _DEFER
    return hits[0]


_SEX_TOKEN = re.compile(
    r"\b(?:males?|females?|men|women|man|woman|boys?|girls?)\b"
    r"|(?:^|[;\t|,])\s*(?:[a-z]+[\s_-]+){0,3}(?:sex|gender)\s*[:=]"
    r"|(?:^|[;\t|,])\s*(?:patient|donor|subject|individual|pt)\s*[:=]\s*[MF]\b"
    r"|\b[MF]\s*/\s*[MF]\b",
    re.IGNORECASE,
)


_SEX_RAW_FIELDS = (
    "gsm_title",
    "title",
    "source_name",
    "source",
    "characteristics",
    "treatment_protocol",
    "description",
    "source_name_ch1",
    "source_name_ch2",
    "characteristics_ch1",
    "characteristics_ch2",
    "treatment_protocol_ch1",
    "treatment_protocol_ch2",
)


def _sex_grounded(raw: Dict) -> bool:
    blob = " ".join(str(raw.get(k, "") or "") for k in _SEX_RAW_FIELDS)
    return bool(_SEX_TOKEN.search(blob))


def _ground_sex(val: str, raw: Dict) -> str:
    if val and val.strip().lower() in ("male", "female") and not _sex_grounded(raw):
        return NS
    return val


def extract_sex(raw: Dict) -> str:
    fast = _fast_sex(raw)
    if fast is not _DEFER:
        return _ground_sex(_normalize_ns(fast), raw)
    args = _args_from_raw(raw)
    out = _predict_label(_SIG_SEX, _PROMPT_SEX, "sex", args)
    return _ground_sex(_normalize_ns(out), raw)


def extract_age(raw: Dict) -> str:
    args = _args_from_raw(raw)

    out = _normalize_ns(_predict_label(_SIG_AGE, _PROMPT_AGE, "age", args))
    return out


_LABEL_POOL = None
_LABEL_POOL_LOCK = threading.Lock()


def _label_pool() -> ThreadPoolExecutor:
    """Process-wide pool for the per-label fan-out, created once.

    Sized by LABEL_POOL_SIZE (default 256): enough for the caller's worker
    count times the labels per sample, so full concurrency is preserved. When
    every thread is busy the fan-out simply waits, which is correct
    backpressure — the alternative, spawning threads on demand, is what
    exhausted the pid ceiling. Label tasks only perform HTTP and never submit
    back into this pool, so there is no risk of self-deadlock.
    """
    global _LABEL_POOL
    if _LABEL_POOL is None:
        with _LABEL_POOL_LOCK:
            if _LABEL_POOL is None:
                _LABEL_POOL = ThreadPoolExecutor(
                    max_workers=int(os.environ.get("LABEL_POOL_SIZE", "256")),
                    thread_name_prefix="label",
                )
    return _LABEL_POOL


class Phase1Agent:
    """Phase 1 agent."""

    TOOLS = {
        "Tissue": extract_tissue,
        "Condition": extract_condition,
        "Treatment": extract_treatment,
        "Sex": extract_sex,
        "Age": extract_age,
    }

    def extract(self, raw: Dict) -> Dict[str, str]:

        n = max(
            1,
            min(int(os.environ.get("LABEL_COL_WORKERS", "3") or "3"), len(self.TOOLS)),
        )

        def _do(col):

            return col, self.TOOLS[col](raw)

        out: Dict[str, str] = {}
        if n <= 1:
            for col in self.TOOLS:
                out[col] = _do(col)[1]
            return out
        for col, val in _label_pool().map(_do, list(self.TOOLS.keys())):
            out[col] = val
        return out

    def extract_field(self, raw: Dict, col: str) -> str:
        tool = self.TOOLS.get(col)
        if tool is None:
            return NS

        return tool(raw)


__all__ = [
    "NS",
    "extract_tissue",
    "extract_condition",
    "extract_treatment",
    "extract_sex",
    "extract_age",
    "Phase1Agent",
]
