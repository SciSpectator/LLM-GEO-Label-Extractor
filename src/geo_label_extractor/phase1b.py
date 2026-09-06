"""Phase 1b — per-GSE, per-label context inference agents.

Three independent Ollama calls (Tissue / Condition / Treatment) re-annotate
samples whose Phase 1 output is ``Not Specified`` using the enclosing GSE's
title/summary/design as the SYSTEM prompt — Ollama caches the system-prompt
KV tensors across samples within one GSE, giving the ~40% speed-up.

Public API:
    NS, is_ns
    _llm_call_think_off                      (low-level HTTP call)
    _parse_single_label                      (output parser)
    GSEInferencer(gse_id, gse_meta, ollama_url).infer_sample(gsm, raw, labels)
    Phase1bRecovery().infer_sample(gsm, raw, labels, gse_ctx)
                                             (thin wrapper for pipeline use)
"""

from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Dict, List, Optional

NS = "Not Specified"
LABEL_COLS = ["Tissue", "Condition", "Treatment"]


_CONTROL_MARKER = re.compile(
    r"\b(controls?|ctrls?|untreated|non[-\s]?treated|nt|mock|vehicle|vectors?|"
    r"empty[-\s]?vector|non[-\s]?targeting|non[-\s]?silencing|scrambl\w*|"
    r"sh[-\s]?ctrl|sh[-\s]?control|si[-\s]?ctrl|si[-\s]?scr\w*|parental|"
    r"wild[-\s]?type|wt|na[iï]ve|baseline|unstimulated|unstim|sham|normal)\b",
    re.IGNORECASE,
)


_ECHO_MARKER = re.compile(
    r"sibling samples in this gse|from phase\s*1|phase\s*1 value|output exactly|"
    r"cohort[-\s]?grouping|class[-\s]?1 intervention|experiment context",
    re.IGNORECASE,
)


_DISEASE_PHRASE = re.compile(
    r"\b((?:[A-Za-z]{2,}\s+){1,3}(?:cancer|carcinoma|adenocarcinoma|leukemia|"
    r"leukaemia|lymphoma|sarcoma|melanoma|glioma|glioblastoma|neoplasm|tumou?r|"
    r"myeloma|blastoma|mesothelioma))\b",
    re.IGNORECASE,
)

_DISEASE_NEG = re.compile(
    r"(?:^|\W)(?:no|non|without|negative for|free of|absence of)\W*$", re.IGNORECASE
)


_DISEASE_WORD = re.compile(
    r"\b([A-Za-z]*(?:adenocarcinoma|carcinoma|leukemia|leukaemia|lymphoma|"
    r"sarcoma|melanoma|glioblastoma|glioma|blastoma|myeloma|mesothelioma|"
    r"cancer|neoplasm))\b",
    re.IGNORECASE,
)
_STRIP_LEAD = re.compile(
    r"^(human|mouse|murine|rat|primary|the|a|an|of|with|in|and|for)\s+", re.I
)


_TREAT_STOP = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "cells",
    "cell",
    "control",
    "controls",
    "ctrl",
    "vehicle",
    "untreated",
    "mock",
    "none",
    "not",
    "specified",
    "day",
    "days",
    "hour",
    "hours",
    "week",
    "weeks",
    "dose",
    "treatment",
    "treated",
    "group",
    "sample",
    "nm",
    "um",
    "mm",
    "mg",
    "ml",
    "ug",
    "ng",
    "gy",
    "stimulation",
    "exposure",
    "time",
    "normal",
    "normally",
}


def _treat_grounded(val: str, own: str) -> bool:
    """True if the inferred treatment's content tokens appear in the sample's own
    text; a value with no content tokens (pure control/dose) is grounded."""
    toks = [
        t
        for t in re.findall(r"[a-z][a-z0-9\-]{2,}|[a-z]*\d[a-z0-9]*", str(val).lower())
        if t not in _TREAT_STOP
    ]
    if not toks:
        return True
    own_l = re.sub(r"[^a-z0-9]", " ", str(own).lower())
    own_s = re.sub(r"[^a-z0-9]", "", str(own).lower())
    words = set(own_l.split())
    for t in toks:
        ts = re.sub(r"[^a-z0-9]", "", t)
        if t in words or t in own_l or (len(ts) >= 3 and ts in own_s):
            return True
    return False


def _own_text(raw: dict) -> str:
    return " ".join(
        str(raw.get(k, "") or "")
        for k in ("gsm_title", "source_name", "characteristics")
    )


_HEALTHY_MARKER = re.compile(
    r"\b(healthy|normal|non[-\s]?tumou?r|non[-\s]?diseased?|non[-\s]?malignant|"
    r"unaffected|uninvolved|uninflamed|disease[-\s]?free)\b",
    re.IGNORECASE,
)


def _is_bare_control(v) -> bool:
    """True if v is only a control/normal/healthy state marker with no disease."""
    v = str(v or "").strip()
    if not v:
        return False
    if _DISEASE_PHRASE.search(v):
        return False
    return bool(_CONTROL_MARKER.search(v) or _HEALTHY_MARKER.search(v))


def _recover_disease(text: str):
    """Pull an explicit '<Adj> <Organ> Cancer/Carcinoma/...' phrase from raw
    text, e.g. for a sample whose Condition collapsed to a bare cell-line code."""
    t = text or ""
    m = _DISEASE_PHRASE.search(t) or _DISEASE_WORD.search(t)
    if not m:
        return None

    if _DISEASE_NEG.search(t[: m.start()]):
        return None
    phrase = _STRIP_LEAD.sub("", m.group(1).strip())
    return phrase or None


EXTRACTION_MODEL = os.environ.get("PHASE1_MODEL", "gemma4-e2b-text:latest")
DEFAULT_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LLM_NUM_CTX = int(os.environ.get("LLM_NUM_CTX", "8192"))


_GSE_BLOCK = (
    "Experiment context for THIS sample's GSE:\n"
    "  Title:   {GSE_TITLE}\n"
    "  Summary: {GSE_SUMMARY}\n"
    "  Design:  {GSE_DESIGN}\n"
    "Sibling distribution for this label across other samples in the same\n"
    "GSE is provided in the user message. Treat it as cohort evidence, not\n"
    "as a substitute for THIS sample's own text.\n"
)

_LEGEND_RULE = (
    "When a characteristics field embeds its own value-to-meaning legend\n"
    "in the field name (e.g. the field name lists code-to-label mappings\n"
    'such as "0 = ...; 1 = ...; 2 = ..." before the value), report the\n'
    "meaning that THIS sample's value maps to in that legend, not the raw\n"
    "code and not the full legend text. Use only the mapping as it is\n"
    "written in this sample's own metadata; do not invent codings.\n"
)

_DEMO_RULE = (
    "Demographic identifiers — age, sex / gender, race or ethnicity, BMI,\n"
    "height, and weight — describe the subject, not a clinical state. Do\n"
    "not place demographic values into the condition output.\n"
)

_TREATMENT_DEFINITION = (
    "TREATMENT — UNIVERSAL DEFINITION (applies to every prompt that\n"
    "extracts a Treatment value). A Treatment is any INTERVENTION or\n"
    "EXPOSURE — applied to, experienced by, or recorded as a property\n"
    "of — the biological sample's source organism / tissue / cell line,\n"
    "that is hypothesised to influence the molecular state being\n"
    "measured. The category is the union of two structural classes:\n"
    "  CLASS-1 INTERVENTION (explicitly administered before the assay):\n"
    "    chemical / pharmacological agents and their doses; genetic\n"
    "    edits (gain- or loss-of-function constructs, knockouts,\n"
    "    transgenes, RNA-interference); physical / environmental\n"
    "    challenges (radiation, temperature, gas composition, mechanical\n"
    "    or chemical insult); clinical procedures performed for the\n"
    "    study.\n"
    "  CLASS-2 EXPOSURE / LIFESTYLE COVARIATE (a subject-level history\n"
    "    or status that the study uses as a grouping variable): any\n"
    "    behavioural, dietary, occupational, environmental, or clinical\n"
    "    history captured as a sample characteristic that the cohort\n"
    "    design contrasts — including coded categorical statuses with\n"
    "    embedded legend definitions of the form `<field>(<k1=name1,\n"
    "    k2=name2, ...>): <code>`.\n"
    "RULES (apply structurally, no entity hardcoding):\n"
    "  R-A. When a metadata field carries an INLINE LEGEND of the form\n"
    "       `<field-name> (<k1>=<word1>, <k2>=<word2>, ...): <code>`,\n"
    "       resolve the code to the corresponding word and emit\n"
    "       `<word> <field-name>` (or just <word> if the field-name is\n"
    "       redundant with the word). Never emit the raw code (digit\n"
    "       / yes / no / Y / N / 0 / 1 / 2 / positive / negative).\n"
    "  R-B. Every CLASS-2 status — including the BASELINE/ZERO category\n"
    "       (e.g. `never`, `none`, `0`, `negative`) — is a valid\n"
    "       Treatment value when the cohort uses that status as a study\n"
    "       contrast: emit the decoded category, NOT Not Specified.\n"
    "  R-C. Multiple treatments → join with `; ` in the order they\n"
    "       appear; preserve dose / duration tokens attached to\n"
    "       CLASS-1 agents.\n"
    "EXCLUDED (never a Treatment, regardless of source field name):\n"
    "  (i)  molecular-assay protocol text — bisulfite / chemical\n"
    "       conversion, hybridisation, library construction, fragmentation,\n"
    "       amplification, sequencing or scan protocol, kit / reagent /\n"
    "       instrument names; (ii) demographic descriptors — age, sex,\n"
    "       race, ethnicity, height, weight (raw); (iii) disease /\n"
    "       phenotype names (those belong to Condition); (iv) tissue\n"
    "       / anatomical descriptors (those belong to Tissue);\n"
    "       (v) sample-handling lab protocols (RNA / DNA extraction,\n"
    "       reverse transcription).\n"
    "When NO CLASS-1 intervention AND NO CLASS-2 cohort-grouping\n"
    "covariate is present, output exactly: Not Specified.\n"
)

_REFINE_RULE = (
    "REFINEMENT MODE — the user message includes a `Phase 1 value:` line\n"
    "carrying the verbatim string Phase 1 already extracted for THIS\n"
    "label. When that value is non-empty and not `Not Specified`, run\n"
    "the following structural checks IN ORDER and apply the FIRST one\n"
    "that fires; if none fires, emit the Phase 1 value verbatim.\n"
    "  Check 1 — ACRONYM EXPANSION (purely structural, no prior\n"
    "    knowledge). If the Phase 1 value is a sequence of N capital\n"
    "    letters (optionally with internal hyphens or digits), AND THIS\n"
    "    sample's title/source/characteristics OR the GSE title/summary/\n"
    "    design contains an N-word phrase whose successive words begin\n"
    "    with those same N letters in the same order (case-insensitive),\n"
    "    output that N-word phrase in the form it appears in the text.\n"
    "    The match is by initial letters only — do NOT use any prior\n"
    "    knowledge of what the acronym 'usually' means.\n"
    "  Check 2 — FRAGMENT TO FULLER PHRASE. If the supplied text\n"
    "    contains a longer phrase that literally CONTAINS the Phase 1\n"
    "    value as a substring AND adds a head noun or anatomical /\n"
    "    pathological qualifier (e.g. an organ-adjective whose noun\n"
    "    appears in the title, or a code whose label is supplied by a\n"
    "    legend in the same field), output the longer phrase.\n"
    "  Check 3 — otherwise, emit the Phase 1 value VERBATIM.\n"
    "Use NO prior knowledge of what abbreviations conventionally mean,\n"
    "what brand names map to, what diseases are usually called, or any\n"
    "expansion that is not literally written in the supplied text. The\n"
    "REFINEMENT replacement preserves the SAME entity — it never\n"
    "introduces a different disease, organ, or compound.\n"
    "When the Phase 1 value is `Not Specified` or empty, ignore this\n"
    "REFINEMENT MODE block and apply the inference rules below.\n"
)

_TISSUE_INFER_SYSTEM = (
    "Infer THIS sample's TISSUE — the tissue, organ, cell type, or cell\n"
    "line that this sample is drawn from — when its own metadata fields\n"
    "(title, source, characteristics) did not yield a tissue at the\n"
    "verbatim-extraction step.\n\n"
    "The tissue must describe THIS sample's biological source. When the\n"
    "sample's own text names a tissue/organ/cell type/cell line, that is\n"
    "the answer. When the sample text gives no tissue signal, the GSE\n"
    "context and sibling distribution may indicate that all samples in\n"
    "this experiment share one tissue — in that case the shared tissue\n"
    "applies to this sample too. When the experiment compares multiple\n"
    "tissues and this sample's text does not pick one, output exactly:\n"
    "Not Specified.\n\n"
    "LAST-RESORT FALLBACK — disease implies organ. Apply only when ALL\n"
    "three conditions hold:\n"
    "  (a) THIS sample's own title / source / characteristics name no\n"
    "      tissue, organ, cell type, cell line, bodily fluid (blood,\n"
    "      plasma, serum, lymph, cerebrospinal fluid, urine, saliva,\n"
    "      bile), excreta, secretion, scraping, or any anatomical\n"
    "      specimen — i.e. the sample text is fully silent about the\n"
    "      biological source; AND\n"
    "  (b) the GSE title / summary / design and the sibling distribution\n"
    "      do not pin a single shared tissue; AND\n"
    "  (c) the Phase-1 Condition value provided in the user message names\n"
    "      a disease that is anatomically defined — its name itself denotes\n"
    "      a specific organ or tissue (e.g. an organ-adjective plus a\n"
    "      pathology noun, or a pathology of a single named organ).\n"
    "Then output the implied organ / tissue noun (not the disease name).\n"
    "Do NOT apply this fallback when:\n"
    "  • the disease is not organ-specific — systemic, metabolic,\n"
    "    hematologic, neurodevelopmental, autoimmune, or otherwise\n"
    "    multi-organ — i.e. it can occur in many tissues without one\n"
    "    defining site;\n"
    "  • the sample text or GSE context indicates the material was drawn\n"
    "    from a site other than the disease's primary organ (e.g. a\n"
    "    peripheral / surrogate biopsy in a study of an organ-defined\n"
    "    disease) — in that case stay with Not Specified;\n"
    "  • the Condition itself is Not Specified or absent.\n"
    "When this fallback does not apply, output Not Specified.\n\n"
    + _REFINE_RULE
    + "\n"
    + _GSE_BLOCK
    + "\n"
    + _LEGEND_RULE
    + '\nIf multiple tissues apply to this sample, join them with "; " in the\n'
    "order they appear."
)

_CONDITION_INFER_SYSTEM = (
    "Infer THIS sample's CONDITION — a disease, pathological phenotype,\n"
    "disease stage / grade / severity marker, OR explicit healthy /\n"
    "control / normal sample state — when its own metadata fields did\n"
    "not yield a condition at the verbatim-extraction step.\n\n"
    "TREATMENT-ARM CONTROL NEVER REPLACES A STATED DISEASE (highest\n"
    "priority): if THIS sample is itself a diseased cell line / tumor /\n"
    "patient sample (a disease, cancer, tumor, or pathology phrase is\n"
    "present in its own text) AND also carries an experimental-arm control\n"
    "marker (untreated, no treatment, vehicle, DMSO control, mock, empty\n"
    "vector, vector control, control construct, scrambled, non-targeting,\n"
    "shControl, siControl, siNC, negative control, wild-type/parental arm),\n"
    "that control marker describes the TREATMENT arm, NOT the disease\n"
    "status — the cells STILL HAVE the disease. Output the DISEASE; NEVER\n"
    "output the bare word 'control' / 'untreated' / 'vehicle' / 'vector' as\n"
    "the Condition when a disease is also present for this sample. This does\n"
    "NOT change a genuine SUBJECT control (healthy donor, disease state:\n"
    "healthy / normal, affection_status: unaffected) that has no disease of\n"
    "its own — that still yields 'healthy' / 'normal' / 'control'.\n\n"
    "WHAT COUNTS AS A CONDITION (THREE EQUAL-PRIORITY CATEGORIES):\n"
    "  (1) Disease names, phenotypes, stage / grade / severity markers.\n"
    "  (2) Explicit healthy / control / normal sample state. When the\n"
    "      sample's own text or a `disease state:` / `condition:` /\n"
    "      `affection_status:` / `group:` field literally contains\n"
    "      'control', 'normal', 'healthy', 'non-tumor', 'non-disease',\n"
    "      'unaffected', 'uninflamed', 'uninvolved', or 'healthy donor'\n"
    "      AS THIS sample's own state, EMIT that state verbatim. Do NOT\n"
    "      emit Not Specified when such a marker is documented for this\n"
    "      sample.\n"
    "  (3) Pathological phenotype markers without a named disease\n"
    "      ('tumor', 'lesion', 'metastasis', 'primary tumor').\n\n"
    "The condition must apply to THIS sample. The broader GSE topic is\n"
    "not by itself evidence — many studies include controls, siblings,\n"
    "or unaffected family members. Use GSE context and sibling\n"
    "distribution to interpret what the sample text means, not to\n"
    "override it. If the GSE studies a disease but THIS sample's text\n"
    "says 'control' / 'healthy donor' / 'non-tumor', THIS sample's\n"
    "Condition is the control state, NOT the GSE disease. If the sample\n"
    "text gives no condition signal AT ALL (no disease, no stage, no\n"
    "control / normal / healthy marker) and the GSE topic alone is the\n"
    "only cue, output exactly: Not Specified.\n\n"
    "EXCLUSIONS (a disease MENTION is NOT this sample's condition):\n"
    "  - FIELD-LEVEL DENIAL: a disease-named field with a denial value\n"
    "    (N, No, 0, false, negative, absent, unaffected, none, neg) is\n"
    "    a denial of THAT field only. Does NOT apply to biomarker /\n"
    "    receptor / molecular-marker / haplotype fields whose name is\n"
    "    a gene, protein, receptor, or marker.\n"
    "  - RELATIONAL: a disease name inside a relational phrase\n"
    '    ("sibling of", "relative of", "parent of", "child of",\n'
    '    "family member of", "unaffected ... of", "proband for",\n'
    '    "control for", "donor for", "healthy donor for") belongs\n'
    "    to the proband, NOT this sample. Look for THIS sample's own\n"
    "    `disease state` / `affection_status` field instead.\n"
    "    DISAMBIGUATION: 'control' / 'healthy' / 'donor' appearing\n"
    "    AS THIS sample's own state — without a relational phrase\n"
    "    naming a different subject — IS this sample's Condition. The\n"
    "    relational rule fires only when a different subject's disease\n"
    "    follows the relational opener.\n"
    "  - PRIORITY: an explicit `diagnosis:` / `dx:` / `condition:` /\n"
    "    `disease state:` / `pathology:` / `affection_status:` /\n"
    "    `group:` field with a non-empty value WINS — whether the value\n"
    "    is a disease ('breast cancer') or a control state ('control',\n"
    "    'healthy', 'normal'), it IS the condition.\n\n"
    + _REFINE_RULE
    + "\n"
    + _GSE_BLOCK
    + "\n"
    + _LEGEND_RULE
    + "\n"
    + _DEMO_RULE
    + "\nIf multiple conditions apply to this sample (primary disease plus\n"
    "comorbidities, additional diagnoses, or co-existing pathologies),\n"
    'join them with "; " in the order they appear.'
)

_TREATMENT_INFER_SYSTEM = (
    "Infer THIS sample's TREATMENT — the drug, compound, genetic\n"
    "perturbation, exposure, or clinical / experimental procedure applied\n"
    "to this sample — when its own metadata fields did not yield a\n"
    "treatment at the verbatim-extraction step.\n\n"
    "The treatment must apply to THIS sample. The broader GSE topic is\n"
    "not by itself evidence that this sample received the treatment —\n"
    "many studies include untreated controls, vehicle arms, or baseline\n"
    "samples. If the sample text gives no treatment signal, output\n"
    "exactly: Not Specified.\n\n"
    "SHARED-PROTOCOL CONTROL-ARM GUARD (control-leak, critical): the GSE\n"
    "context and a shared treatment_protocol describe the TREATED arms of\n"
    "the whole experiment. When THIS sample's OWN title / source /\n"
    "characteristics mark it as a control / vehicle / untreated / mock /\n"
    "vector / empty-vector / non-targeting / scrambled / shControl /\n"
    "parental / wild-type / naive / baseline / 'NT' / normal arm, do NOT\n"
    "assign the active drug(s) or perturbation(s) that the context\n"
    "describes for the treated arms. Emit only a literally-named control\n"
    "substring, else Not Specified. Never infer a drug from world\n"
    "knowledge about the cell line, disease, or GSE topic.\n\n"
    + _TREATMENT_DEFINITION
    + "\n\n"
    + _REFINE_RULE
    + "\n"
    + _GSE_BLOCK
    + "\n"
    + _LEGEND_RULE
    + '\nIf multiple treatments apply to this sample, join them with "; " in\n'
    "the order they appear."
)

_PER_LABEL_INFER_SYSTEMS = {
    "Tissue": _TISSUE_INFER_SYSTEM,
    "Condition": _CONDITION_INFER_SYSTEM,
    "Treatment": _TREATMENT_INFER_SYSTEM,
}


def is_ns(text: str) -> bool:
    if text is None:
        return True
    return str(text).lower().strip() in {
        "not specified",
        "n/a",
        "none",
        "unknown",
        "na",
        "not available",
        "not applicable",
        "unclear",
        "unspecified",
        "missing",
        "undetermined",
        "insufficient",
        "insufficient information",
        "no information",
        "no data",
        "",
    }


_ARTIFACT_RX = __import__("re").compile(r"\[\[[^\[\]]*\]\]")


def _strip_artifacts(value: str) -> str:
    """Strip leaked DSPy/gemma marker artifacts (e.g. ``[[ ## completed ]]``)."""
    return _ARTIFACT_RX.sub("", value).strip()


_LABEL_PREFIX_RX = __import__("re").compile(
    r"^\s*(?:treatment|tissue|condition|disease|sex|gender|age|drug|compound|"
    r"answer|label|value|result|output)s?\s*[:—–|]\s*",
    __import__("re").IGNORECASE,
)


def _strip_label_prefix(value: str) -> str:
    prev = None
    v = value or ""
    while v != prev:
        prev = v
        v = _LABEL_PREFIX_RX.sub("", v, count=1).strip()
    return v


def _parse_single_label(text: str) -> str:
    """Parse label(s) from a per-label LLM agent response.
    Supports multiple values separated by semicolons.
    """
    if not text:
        return NS
    text = _strip_artifacts(text)
    if "{" in text:
        import re as _re

        m = _re.search(r"\{.*\}", text, _re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                for v in data.values():
                    v = str(v).strip()
                    if v and v.lower() not in (
                        "none",
                        "null",
                        "",
                        "not specified",
                        "n/a",
                        "unknown",
                    ):
                        return v
            except Exception:
                pass
    for line in text.splitlines():
        line = line.strip().rstrip(".")
        if line.lower().startswith(("note:", "rules:")):
            line = line.split(":", 1)[1].strip() if ":" in line else ""
        line = _strip_label_prefix(line)
        if not line or line.lower() in (
            "none",
            "null",
            "",
            "not specified",
            "n/a",
            "unknown",
        ):
            continue
        if ";" in line:
            parts = [
                _strip_label_prefix(p.strip().rstrip(".")) for p in line.split(";")
            ]
            parts = [
                p
                for p in parts
                if p
                and p.lower()
                not in ("none", "null", "", "not specified", "n/a", "unknown")
            ]
            if parts:
                return "; ".join(parts)
        return line
    return NS


_tls = threading.local()


def _llm_call_think_off(
    model: str,
    prompt: str,
    ollama_url: str = DEFAULT_URL,
    max_tokens: int = 1024,
    system: str = "",
) -> str:
    """Ollama chat call for phase 1b inference. Reasoning (think) is controlled
    by the PHASE1B_THINK env (default OFF); function name preserved for backward
    import compatibility. Setting ``LLM_BACKEND=vllm`` switches to vLLM's
    OpenAI-compatible endpoint without touching the Ollama path.
    """
    messages: List[Dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    if os.environ.get("LLM_BACKEND", "ollama").lower() in ("vllm", "sglang", "openai"):
        from llm_backend import chat as _vllm_chat

        return _vllm_chat(
            messages,
            model=model,
            temperature=0.0,
            seed=42,
            num_predict=max_tokens,
            num_ctx=LLM_NUM_CTX,
            timeout=180,
            retries=3,
        )

    url = ollama_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "options": {
            "temperature": 0.0,
            "num_predict": max_tokens,
            "num_ctx": LLM_NUM_CTX,
        },
        "think": os.environ.get("PHASE1B_THINK", "false").lower()
        in ("1", "true", "yes"),
        "stream": False,
        "keep_alive": -1,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    attempt = 0
    malformed = 0
    while True:
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("message", {}).get("content", "").strip()
        except urllib.error.HTTPError as e:

            if 400 <= e.code < 500 and e.code not in (408, 429):
                print(f"[p1b FATAL] permanent HTTP {e.code}: {e!r}", flush=True)
                raise
            attempt += 1
            if attempt % 20 == 0:
                print(
                    f"[p1b retry] {attempt} transient retries; last={e!r}",
                    flush=True,
                )
            time.sleep(min(60.0, 2.0 * attempt))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:

            attempt += 1
            if attempt % 20 == 0:
                print(
                    f"[p1b retry] {attempt} transport retries; last={e!r}",
                    flush=True,
                )
            time.sleep(min(60.0, 2.0 * attempt))
        except json.JSONDecodeError as e:

            malformed += 1
            if malformed > 5:
                print(
                    f"[p1b FATAL] {malformed} malformed responses; last={e!r}",
                    flush=True,
                )
                raise
            print(
                f"[p1b retry] malformed response #{malformed}; last={e!r}",
                flush=True,
            )
            time.sleep(min(30.0, 2.0 * malformed))


def _fmt_sibling_dist(dist: Optional[Counter], top_k: int = 5) -> str:
    if not dist:
        return "(none)"
    items = sorted(dist.items(), key=lambda x: -x[1])[:top_k]
    return "; ".join(f'"{v}" (n={n})' for v, n in items)


class GSEInferencer:
    """Phase 1b — Per-GSE, per-label inference agents with KV cache reuse.

    3 independent LLM agents (one per label column), each with its OWN
    system prompt containing THIS GSE's context only.  Ollama caches the
    system-prompt KV tensors per GSE → ~40% faster across samples of the
    same experiment.

    One GSEInferencer instance per GSE — NOT shared across GSEs.
    """

    def __init__(self, gse_id: str, gse_meta: dict, ollama_url: str = DEFAULT_URL):
        self.gse_id = gse_id
        self.url = ollama_url
        _title = gse_meta.get("gse_title") or gse_meta.get("title", "") or ""
        _summary = gse_meta.get("gse_summary") or gse_meta.get("summary", "") or ""
        _design = gse_meta.get("gse_design") or gse_meta.get("design", "") or ""
        self._systems: Dict[str, str] = {}
        for col in LABEL_COLS:
            tmpl = _PER_LABEL_INFER_SYSTEMS.get(col, "")
            self._systems[col] = (
                tmpl.replace("{GSE_TITLE}", _title)
                .replace("{GSE_SUMMARY}", _summary)
                .replace("{GSE_DESIGN}", _design)
            )

    def _llm_call(self, system: str, user_msg: str, max_tokens: int = 1024) -> str:
        return _llm_call_think_off(
            EXTRACTION_MODEL, user_msg, self.url, max_tokens=max_tokens, system=system
        )

    def infer_sample(
        self,
        gsm: str,
        raw: dict,
        current_labels: Dict[str, str],
        cols: Optional[List[str]] = None,
        sibling_dist: Optional[Dict[str, Counter]] = None,
    ) -> Dict[str, str]:
        """Infer missing labels — fans out per-column calls in parallel
        (3 cols → up to 3 concurrent LLM calls per sample). Stays
        per-sample so the outer pool's cross-sample concurrency still
        composes; with OLLAMA_NUM_PARALLEL>=8 the runner has enough
        slots to serve column calls without starving the queue.

        ``sibling_dist`` is a per-field Counter of values observed across
        OTHER samples in the same GSE (this sample's own value already
        excluded). Embedded in the user message as the cohort signal.
        """
        from concurrent.futures import ThreadPoolExecutor

        _cols = cols or LABEL_COLS

        _n = max(
            1, min(int(os.environ.get("LABEL_COL_WORKERS", "3") or "3"), len(_cols))
        )
        _title = str(raw.get("gsm_title", "")).strip()
        _source = str(raw.get("source_name", "")).strip()
        _char = str(raw.get("characteristics", "")).replace("\t", " ").strip()
        sib_per_field = sibling_dist or {}

        _own = f"{_title} {_source} {_char}"
        _own_is_control = bool(_CONTROL_MARKER.search(_own))

        def _do_col(col: str) -> tuple[str, str]:
            sib_line = _fmt_sibling_dist(sib_per_field.get(col))
            phase1_val = str(current_labels.get(col, NS) or NS).strip() or NS

            if col == "Treatment" and _own_is_control:
                return col, phase1_val
            parts = [
                f"Title: {_title}",
                f"Source: {_source}",
                f"Characteristics: {_char}",
                "",
                f"Phase 1 value: {phase1_val}",
            ]
            if col == "Tissue":
                cond_now = str(current_labels.get("Condition", NS) or NS).strip() or NS
                trt_now = str(current_labels.get("Treatment", NS) or NS).strip() or NS
                parts.append("")
                parts.append(f"Condition (from Phase 1): {cond_now}")
                parts.append(f"Treatment (from Phase 1): {trt_now}")
            parts.append("")
            parts.append(f"Sibling samples in this GSE ({col} from Phase 1):")
            parts.append(f"  {sib_line}")
            parts.append("ANSWER:")
            user_msg = "\n".join(parts)
            sys_prompt = self._systems.get(col, "")
            text = ""
            for attempt in range(3):
                text = self._llm_call(sys_prompt, user_msg, max_tokens=1024)
                if text:
                    break
                time.sleep(3 * (attempt + 1))
            try:
                val = _parse_single_label(text)

                if val and _ECHO_MARKER.search(str(val)):
                    return col, current_labels.get(col, NS)

                if col == "Condition" and val and not is_ns(val):
                    v = str(val).strip()
                    is_code = bool(
                        re.fullmatch(r"[A-Za-z]{1,6}[-\s]?\d{1,5}[A-Za-z0-9]*", v)
                    )
                    if is_code and not _DISEASE_PHRASE.search(v):
                        dis = _recover_disease(_own)
                        if dis:
                            return col, dis

                if col == "Treatment" and val and not is_ns(val):
                    if not _treat_grounded(val, _own):
                        return col, phase1_val

                if col == "Condition" and val and _is_bare_control(val):
                    _pv = str(phase1_val or "").strip()
                    if _pv and not is_ns(_pv) and not _is_bare_control(_pv):
                        return col, phase1_val
                    _dis = _recover_disease(_own)
                    if _dis:
                        return col, _dis
                if not is_ns(val):
                    return col, val
            except Exception:
                pass
            fb = current_labels.get(col, NS)

            if col == "Condition" and fb and not is_ns(fb):
                v = str(fb).strip()
                if re.fullmatch(
                    r"[A-Za-z]{1,6}[-\s]?\d{1,5}[A-Za-z0-9]*", v
                ) and not _DISEASE_PHRASE.search(v):
                    dis = _recover_disease(_own)
                    if dis:
                        return col, dis
            if col == "Condition" and fb and _is_bare_control(fb):
                _dis = _recover_disease(_own)
                if _dis:
                    return col, _dis
            return col, fb

        updated = dict(current_labels)
        if _n <= 1:
            for col in _cols:
                col, val = _do_col(col)
                updated[col] = val
            return updated
        with ThreadPoolExecutor(max_workers=_n) as ex:
            for col, val in ex.map(_do_col, list(_cols)):
                updated[col] = val
        return updated


class Phase1bRecovery:
    """Thin wrapper. Maintains a GSEInferencer per GSE id."""

    def __init__(self, ollama_url: str = DEFAULT_URL):
        self.url = ollama_url
        self._infers: Dict[str, GSEInferencer] = {}

    def _inferencer(self, gse_id: str, gse_ctx: dict) -> GSEInferencer:
        inf = self._infers.get(gse_id)
        if inf is None:
            inf = GSEInferencer(gse_id, gse_ctx, self.url)
            self._infers[gse_id] = inf
        return inf

    def infer_sample(
        self,
        gsm: str,
        raw: dict,
        labels: Dict[str, str],
        gse_id: str,
        gse_ctx: dict,
        sibling_dist: Optional[Dict[str, Counter]] = None,
    ) -> Dict[str, str]:
        return self._inferencer(gse_id, gse_ctx).infer_sample(
            gsm, raw, labels, sibling_dist=sibling_dist
        )


__all__ = [
    "NS",
    "LABEL_COLS",
    "EXTRACTION_MODEL",
    "is_ns",
    "_parse_single_label",
    "_llm_call_think_off",
    "GSEInferencer",
    "Phase1bRecovery",
]
