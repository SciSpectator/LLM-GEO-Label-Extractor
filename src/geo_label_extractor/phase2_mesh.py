"""Phase 2 (MeSH-only) collapse driver.

Replaces ``phase2.py`` and ``phase2_pubtator.py``. Resolves each Phase 1
raw label to a canonical MeSH descriptor name, or — if no MeSH
descriptor fits — mints an entry in the out-of-distribution (OOV) mesh
(ART-{T,C,X}-#####), backed by ``mesh.sqlite``'s
``oov_mesh_clusters`` table.

Output rule: returns canonical NAMES only, never MeSH IDs (per the user
spec). For composite Phase 1 labels (semicolon-joined), each component
is resolved independently and the canonical names are joined back with
``; `` in input order.

Tier order per component:

    1. Episodic recall
       Last decision logged for the same (raw_label, col) gets reused.

    2. Exact MeSH match
       ``mesh_terms.name`` or ``mesh_synonyms.synonym`` (NOCASE),
       category-gated by col (Tissue=A, Condition=C/F, Treatment=D/E).
       If exactly 1 hit, take it. If >1 hit, LLM picks among them.

    3. Existing OOV-mesh match
       ``oov_mesh_clusters.label`` or ``oov_mesh_synonyms.synonym``
       (NOCASE).

    4. Hybrid candidate gathering + LLM picker
       Pool MeSH candidates from two complementary sources:
         - PubTator3 normalize:   curated NCBI entity → MeSH (best on
           abbreviations like FTD/PBMC/MPTP and named drugs).
         - BioLORD top-K:         semantic similarity over MeSH names +
           scopes + synonyms (best on free-form anatomy/condition prose
           where PubTator3's NER is brittle, e.g. "whole blood",
           "human liver tissue").
       Both are col-gated to MeSH categories; results are deduped by
       MeSH ID and handed to gemma4:e2b which picks one or replies NONE.

    5. Mint OOV-mesh entry
       If the picker says NONE (or PubTator3 + BioLORD both produced
       nothing), mint a new ART-{T,C,X}-##### entry into the OOV mesh.

NS / empty inputs short-circuit to ``Not Specified``. PubTator3 use is
optional (network-dependent); set ``use_pubtator=False`` for fully
offline operation, in which case Tier 4 falls back to BioLORD-only.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

try:
    import requests as _requests
except ImportError:
    _requests = None

from mesh_lookup import COL_CATS, MeshDB

try:
    from cellline_db import CellLineDB
except Exception:
    CellLineDB = None

try:
    import acronym_expand
except Exception:            # acronym stage degrades to the model-only path
    acronym_expand = None

try:
    from qualifier_guard import is_pure_qualifier
except Exception:            # guard optional; absence restores released behaviour
    def is_pure_qualifier(_value):
        return False

try:
    from phase2_pubtator import PubTatorNormalizer
except ImportError:
    PubTatorNormalizer = None


NS = "Not Specified"
LABEL_COLS = ("Tissue", "Condition", "Treatment")


_CONDITION_CONTROL_CANONICAL: dict[str, str] = {
    "control": "Control",
    "controls": "Control",
    "ctrl": "Control",
    "ctl": "Control",
    "normal": "Normal",
    "healthy": "Healthy",
    "healthy control": "Healthy Control",
    "healthy controls": "Healthy Control",
    "normal control": "Normal Control",
    "normal controls": "Normal Control",
    "healthy donor": "Healthy Donor",
    "untreated": "Untreated",
    "non-treated": "Untreated",
    "nontreated": "Untreated",
    "no treatment": "Untreated",
    "wt": "Wild Type",
    "wild type": "Wild Type",
    "wild-type": "Wild Type",
    "wildtype": "Wild Type",
    "baseline": "Baseline",
    "mock": "Mock",
    "sham": "Sham",
    "naive": "Naive",
    "naïve": "Naive",
    "unstimulated": "Unstimulated",
    "unstim": "Unstimulated",
    "non-tumor": "Non-Tumor",
    "non-tumorous": "Non-Tumor",
    "non-malignant": "Non-Malignant",
    "non-disease": "Non-Disease",
    "unaffected": "Unaffected",
    "uninflamed": "Uninflamed",
    "uninvolved": "Uninvolved",
    "no condition": "Control",
    "no disease": "Control",
    "none": "Control",
}


_TISSUE_GENERIC_PLACEHOLDERS = frozenset(
    {
        "tumor",
        "tumour",
        "tumors",
        "tumours",
        "tissue",
        "tissues",
        "cell",
        "cells",
        "sample",
        "samples",
        "biopsy",
        "biopsies",
        "specimen",
        "specimens",
    }
)

_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
_LLM_MODEL = os.environ.get("PHASE2_MODEL", "gemma4-e2b-text:latest")
_LLM_NUM_CTX = int(os.environ.get("PHASE2_NUM_CTX", "4096"))
_TOP_K = int(os.environ.get("PHASE2_TOPK", "30"))


_PROMPT_VERSION = os.environ.get("PHASE2_PROMPT_VERSION", "1")


_CONTEXT_CHARS = int(os.environ.get("PHASE2_CONTEXT_CHARS", "1200"))


_ANAT_ADJ: dict[str, str] = {
    "hepatic": "liver",
    "renal": "kidney",
    "pulmonary": "lung",
    "cardiac": "heart",
    "gastric": "stomach",
    "cerebral": "brain",
    "splenic": "spleen",
    "intestinal": "intestine",
    "colonic": "colon",
    "esophageal": "esophagus",
    "bronchial": "bronchi",
    "thymic": "thymus",
    "pancreatic": "pancreas",
    "ovarian": "ovary",
    "uterine": "uterus",
    "prostatic": "prostate",
    "mammary": "breast",
    "muscular": "muscle",
    "neuronal": "neuron",
    "vascular": "vessel",
    "osseous": "bone",
    "skeletal": "skeleton",
    "dermal": "skin",
    "epidermal": "epidermis",
    "lingual": "tongue",
    "nasal": "nose",
    "ocular": "eye",
    "biliary": "bile",
    "lymphatic": "lymph",
    "salivary": "saliva",
    "adrenal": "adrenal",
}


_HISTOLOGY_STOP: set[str] = {
    "tissue",
    "tissues",
    "parenchyma",
    "parenchymal",
    "sample",
    "samples",
    "specimen",
    "specimens",
    "cell",
    "cells",
    "biopsy",
    "biopsies",
    "section",
    "sections",
    "block",
    "blocks",
    "of",
    "the",
    "a",
    "an",
}

_TOK_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]+")


_UNIT_TOKEN = (
    r"mg/kg|µg/kg|ug/kg|μg/kg|ng/kg"
    r"|ng/ml|ng/mL|mg/ml|mg/mL|µg/ml|ug/ml|μg/ml|µg/mL|ug/mL|μg/mL"
    r"|U/ml|U/mL|IU/ml|IU/mL"
    r"|mM|nM|µM|uM|μM|pM|fM|M"
    r"|mmol|nmol|µmol|umol|mol"
    r"|mg|µg|ug|μg|ng|pg|kg|g"
    r"|mL|ml|µL|uL|μL|nL|nl|L|l"
    r"|cGy|Gy"
    r"|IU|U"
    r"|hours|hour|hrs|hr"
    r"|mins|min"
    r"|secs|sec"
    r"|days|day"
    r"|weeks|week|wks|wk"
    r"|months|month|mo"
    r"|years|year|yrs|yr"
    r"|(?-i:h|s|d|y)"
)


_DOSE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:" + _UNIT_TOKEN + r")(?![A-Za-z]|-[A-Za-z])"
    r"|\b\d+(?:\.\d+)?\s*%"
    r"|\bfor\s+\d+\s*(?:" + _UNIT_TOKEN + r")(?![A-Za-z]|-[A-Za-z])"
    r"|\b(?:at|after|over)\s+\d+\s*(?:" + _UNIT_TOKEN + r")(?![A-Za-z]|-[A-Za-z])"
    r"|\b\d+\s*(?:fold|x)\b",
    re.IGNORECASE,
)


_SUBTYPE_NON_RE = re.compile(r"^non-[a-z]+(?:-[a-z]+)*\s+\S", re.IGNORECASE)


def _strip_dose(raw: str) -> str | None:
    """Strip numeric dose / duration / concentration / route tokens from
    a Treatment label. Returns the stripped form when it differs from the
    input (case-insensitive); else None.

    Examples:
      'paclitaxel 10 nM 24 h' → 'paclitaxel'
      'cisplatin 5 µg/mL'     → 'cisplatin'
      'metformin 100mg/kg'    → 'metformin'
      'IL-12 (10 ng) + butyrate (0.5 mM)'   → 'IL-12 () + butyrate ()'
        — caller still benefits from MeSH on each component.
      'cisplatin'             → None (no rewrite needed)
    """
    if not raw:
        return None
    s = _DOSE_RE.sub(" ", raw)

    s = re.sub(r"\(\s*\)", " ", s)
    s = re.sub(r"[,\;\-]\s*[,\;\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;-")
    if not s or s.lower() == raw.lower():
        return None
    return s


def _extract_dose(raw: str) -> str:
    """Return the dose / concentration / duration tokens present in a Treatment
    raw — the COMPLEMENT of ``_strip_dose`` — joined by '; ' (order-preserving,
    de-duplicated). Empty string if none. Universal numeric+unit lexicon; NO
    drug-name hardcoding. Lets Phase 2 preserve the per-sample dose alongside
    the normalized drug (Option B): raw 'doxorubicin 5 mg/kg for 24 h' yields
    drug 'Doxorubicin' AND dose '5 mg/kg; for 24 h'.

    Examples:
      'paclitaxel 10 nM 24 h'              -> '10 nM; 24 h'
      'IL-12 (10 ng) + butyrate (0.5 mM)'  -> '10 ng; 0.5 mM'
      'cisplatin'                          -> ''  (no dose present)
    """
    if not raw:
        return ""
    seen: set[str] = set()
    out: list[str] = []
    for m in _DOSE_RE.finditer(raw):
        tok = re.sub(r"\s+", " ", m.group(0).strip())

        tok = re.sub(r"^(?:for|at|after|over)\s+", "", tok, flags=re.IGNORECASE)
        k = tok.lower()
        if k and k not in seen:
            seen.add(k)
            out.append(tok)
    return "; ".join(out)


def _augment_raw_tokens(raw: str) -> set[str]:
    """Tokenise the raw label, lowercase it, drop generic-histology
    stops, and add the noun form of any anatomical adjective. The result
    is a set used for token-overlap scoring against candidate names.
    """
    toks = {t.lower() for t in _TOK_RE.findall(raw or "")}
    aug: set[str] = set()
    for t in toks:
        aug.add(t)
        if t in _ANAT_ADJ:
            aug.add(_ANAT_ADJ[t])
    return aug - _HISTOLOGY_STOP


def _candidate_name_tokens(c: dict) -> set[str]:
    return {t.lower() for t in _TOK_RE.findall(c.get("name") or "")} - _HISTOLOGY_STOP


def _augmented_query(raw: str) -> str | None:
    """Rewrite the raw into a normalised query by (a) replacing each
    anatomical adjective with its noun form and (b) dropping generic
    histology stop tokens. Returns the rewritten string when it differs
    from the original (case-insensitive); otherwise None — letting the
    caller skip the secondary retrieval round-trip when it would just
    re-issue the same query.

    Example: 'pulmonary tissue' → 'lung'; 'renal tissue' → 'kidney';
    'lung parenchyma' → 'lung'; 'lung' → None (no rewrite).
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    out: list[str] = []
    for tok in _TOK_RE.findall(raw):
        low = tok.lower()
        if low in _HISTOLOGY_STOP:
            continue
        out.append(_ANAT_ADJ.get(low, low))
    if not out:
        return None
    rewritten = " ".join(out)
    return rewritten if rewritten.lower() != raw.lower() else None


def _format_context_block(context: str) -> str:
    """Render an optional study-context block for inclusion in the picker
    or verifier user message. Returns an empty string when no context is
    given, so callers can unconditionally splice it in.
    """
    ctx = (context or "").strip()
    if not ctx:
        return ""
    if len(ctx) > _CONTEXT_CHARS:
        ctx = ctx[:_CONTEXT_CHARS].rsplit(" ", 1)[0] + "…"

    ctx = " ".join(ctx.split())
    return f"study context: {ctx}\n\n"


_POLARITY_SYSTEM = (
    "You classify the polarity of a biomedical sample label as either\n"
    "ASSERT or NEGATE.\n"
    "\n"
    "Inputs:\n"
    "  - study context (optional) — free-form text from the experiment's\n"
    "    title / summary / overall_design or per-sample characteristics.\n"
    "    When the raw label contains a coded value (digit, single letter,\n"
    "    short token) that is decoded by an inline legend in the context\n"
    "    (e.g. 'category (1, 2, 3 = control, low-dose, high-dose): 1' →\n"
    "    1 means control, which encodes ABSENCE of the active condition),\n"
    "    use the legend to determine whether the resolved meaning is\n"
    "    asserted or negated. ALWAYS consult the context first when the\n"
    "    raw is digit-only or parens/colon-coded.\n"
    "  - raw label, column.\n"
    "\n"
    "Read the raw as a sentence describing one sample / patient. Ask:\n"
    "does this subject EVER possess / undergo / experience the entity\n"
    "named (or implied) by the raw?\n"
    "\n"
    "ASSERT — yes, at some point in time. Includes:\n"
    "  • plain present (e.g. an organ name, a disease name, a drug name)\n"
    "  • past tense / temporal qualifiers (history of <X>, previous <X>,\n"
    "    post-<X>, ex-<X>, former <X>)\n"
    "  • current / recent (currently on <X>, recently treated with <X>)\n"
    "  • affected / positive (subject is <X>-positive, has <X>, is\n"
    "    <X>-status)\n"
    "ASSERT covers any time the entity held for the subject, even if it\n"
    "is no longer holding now.\n"
    "\n"
    "NEGATE — no, the entity NEVER held for this subject. Any natural-\n"
    "language negation that scopes over the whole entity:\n"
    "  • negative adverbs / determiners (never, no, none, not)\n"
    "  • negative prepositions (without, free of, lacking)\n"
    "  • absence verbs (denies, lacks)\n"
    "  • negative status words (absent, unaffected, refused, declined,\n"
    "    'negative' as a status modifier)\n"
    "  • negative prefixes when they negate (non-, un-, dis- when it\n"
    "    means absence)\n"
    "  • bare 'never' / 'none' / 'no' as the whole label\n"
    "NEGATE encodes the negative arm of a contrast (control / no-\n"
    "exposure / no-treatment / disease-free).\n"
    "\n"
    "Edge cases (rules — NOT entity-specific examples):\n"
    "  • Empty / placeholder raws (n/a, not specified, unknown, ?, '-')\n"
    "    → answer ASSERT (they have their own NS short-circuit elsewhere\n"
    "    in the pipeline; you only see them if that short-circuit\n"
    "    missed; do not double-classify them as NEGATE here).\n"
    "  • Compound raws joined with ';' or '+' — classify the WHOLE\n"
    "    string. If ANY component is asserted, answer ASSERT. NEGATE\n"
    "    only when EVERY component is itself negated.\n"
    "  • Some compound forms beginning with 'non-' name positive\n"
    "    biomedical entities (canonical disease subtypes whose accepted\n"
    "    nomenclature contains 'non-' as part of the entity, not as a\n"
    "    negation operator). For these, a subject HAS the compound\n"
    "    entity → answer ASSERT. Test: if the compound has its own\n"
    "    accepted disease/entity name and a subject can be described\n"
    "    as 'having' it, then ASSERT. If, by contrast, the compound\n"
    "    decomposes cleanly into 'non' + (a known entity that the\n"
    "    subject does NOT have), then NEGATE.\n"
    "  • DO NOT be fooled by noun-phrase grammar. A negated agent-noun\n"
    "    (e.g. 'non-<doer>' or 'never-<doer>' where <doer> means 'one\n"
    "    who does X') describes a person — but the question is about\n"
    "    the ENTITY (the action / exposure / disease), not the social\n"
    "    role. A non-doer never did the action → entity = NEGATE.\n"
    "    Plurality / singularity of the role noun is irrelevant.\n"
    "  • DO NOT be fooled by adjective-of-absence forms. Compounds\n"
    "    ending in '-free', '-naive', or '-negative' applied to an\n"
    "    entity name encode ABSENCE of that entity (e.g. <X>-free =\n"
    "    no <X>; <X>-naive = no prior <X>; <X>-negative = lacks <X>)\n"
    "    → NEGATE.\n"
    "  • SUFFIX TEST for 'non-<X>' / 'no <X>' / '<X>-free' / 'never\n"
    "    <X>' / 'never had <X>' / 'absent <X>' / '<X>-negative' /\n"
    "    'lacking <X>': if the rest of the raw IS the bare entity (or\n"
    "    a clear morphological inflection of it — plural, gerund,\n"
    "    agent-noun derived from a verb) → NEGATE. ONLY treat the\n"
    "    prefixed form as ASSERT when the compound is itself a\n"
    "    recognized biomedical entity that the subject HAS (i.e. the\n"
    "    'non-<X>' compound has its own canonical name in disease /\n"
    "    drug nomenclature, not a literal negation of <X>).\n"
    "  • PARTICIPLE-NEGATION TEST. The prefixes 'un-' and 'non-' on a\n"
    "    PAST PARTICIPLE or on an AGENT NOUN derived from a verb\n"
    "    encode absence of that action — 'un-<verb-ed>' means the\n"
    "    subject did NOT undergo <verb>; 'non-<doer>' means the\n"
    "    subject is NOT the doer of <verb>. Both → NEGATE. (This is\n"
    "    the test that distinguishes 'non-<doer>' from 'non-<disease-\n"
    "    name>': the former is a negated verbal noun, the latter is a\n"
    "    canonical compound entity.)\n"
    "\n"
    "OUTPUT FORMAT (strict): reason in one short line, then on the\n"
    "VERY LAST line output exactly:\n"
    "  POLARITY: ASSERT\n"
    " or\n"
    "  POLARITY: NEGATE\n"
    "The parser requires the literal 'POLARITY:' prefix.\n"
)


_PICKER_SYSTEM = (
    "You are a biomedical normalizer mapping a raw GEO sample label to\n"
    "the best MeSH descriptor. Inputs (in this order, when present):\n"
    "  - study context: free-form text from the experiment's title /\n"
    "    summary / overall_design. ALWAYS read this first; it usually\n"
    "    defines abbreviations, brand names, and study-specific terms\n"
    "    (e.g. an abstract that says 'AD = Alzheimer Disease' or\n"
    "    'patients received DEX (dexamethasone)' tells you exactly\n"
    "    how to expand short forms in the raw label).\n"
    "  - raw label: the surface form to normalize.\n"
    "  - column: Tissue / Condition / Treatment.\n"
    "  - candidates: numbered MeSH descriptors with [category] tag and\n"
    "    short scope. Output one PICK.\n"
    "\n"
    "DEFAULT BEHAVIOUR: pick the candidate that denotes the SAME\n"
    "biomedical entity as the raw — even when wording differs. Reply\n"
    "NONE only when no candidate denotes the same entity.\n"
    "\n"
    "Use the study context to disambiguate:\n"
    "  • If the raw is a short abbreviation (1–6 letters), look in the\n"
    "    context for the spelled-out form, then match the candidate\n"
    "    whose preferred name / scope corresponds to that expansion.\n"
    "  • If the raw is a brand or trade name and the context mentions\n"
    "    its generic active ingredient, pick the generic candidate.\n"
    "  • If multiple candidates fit the raw alone, prefer the one most\n"
    "    consistent with the experiment topic stated in the context.\n"
    "  • If no context is provided, fall back to general biomedical\n"
    "    knowledge to expand abbreviations / brand names.\n"
    "\n"
    "Wording differences that still mean SAME entity (always pick):\n"
    "  • Abbreviation ↔ its standard expansion. Use the study context\n"
    "    first; otherwise general biomedical knowledge.\n"
    "  • Brand / trade name ↔ the generic preferred name of the drug.\n"
    "  • Chemical / IUPAC / systematic name of a known drug ↔ that\n"
    "    drug's preferred name (NOT the parent chemical class).\n"
    "  • Dose / duration / concentration suffix on a compound — treat\n"
    "    the compound alone (strip the dose; the descriptor is the\n"
    "    compound).\n"
    "  • Cell-line / strain / species / age modifier on a tissue —\n"
    "    collapse to the underlying anatomy or cell-type descriptor.\n"
    "  • Adjectival form of an organ ↔ the organ's noun descriptor\n"
    "    (linguistic equivalent: an organ-adjective always names that\n"
    "    organ).\n"
    "  • '<organ-name> tissue' / '<organ-name> parenchyma' /\n"
    "    '<organ-name> sample' ↔ the organ's MeSH descriptor. The\n"
    "    organ-name modifier wins; the trailing generic-histology word\n"
    "    is just sample-source phrasing.\n"
    "  • Synonym / older nomenclature / minor spelling variant of the\n"
    "    same entity.\n"
    "  • Subtype IS explicitly named in the raw → pick that subtype.\n"
    "    Subtype is NOT named in the raw → pick the broader umbrella;\n"
    "    DO NOT invent a subtype the raw never mentions.\n"
    "\n"
    "Generic-histology trap: never pick a category-wide histology\n"
    "umbrella (descriptors whose meaning is just 'tissue', 'cells',\n"
    "'parenchyma', 'connective tissue', 'epithelium' etc.) when the raw\n"
    "names a SPECIFIC organ, drug, or disease. The organ-named\n"
    "candidate always beats the generic-histology candidate.\n"
    "\n"
    "When NOT to pick (reply NONE):\n"
    "  R1. The raw is a placeholder with NO biomedical content (none,\n"
    "      n/a, not specified). These cannot legitimately map to any\n"
    "      MeSH descriptor.\n"
    "      Note: 'control' / 'healthy' / 'normal' / 'untreated' /\n"
    "      'wild type' / 'baseline' for the Condition column are\n"
    "      handled BEFORE the picker (canonicalized to a fixed surface\n"
    "      form) — you will not see them here.\n"
    "  R2. Every candidate is a different specific entity that merely\n"
    "      shares a word with the raw — no candidate denotes the\n"
    "      raw's entity at all.\n"
    "  R3. Every candidate is in the wrong MeSH branch for the column.\n"
    "      Tissue requires category A (anatomy / cell / cell line);\n"
    "      Condition requires C or F (disease / mental disorder);\n"
    "      Treatment requires D or E (drug / biologic / procedure /\n"
    "      therapeutic technique). Cross-branch picks are NEVER valid.\n"
    "  R4. NEGATED RAW. The raw is grammatically negated — read it as\n"
    "      a sentence and ask: does the subject EVER possess / undergo\n"
    "      the entity, or NOT?\n"
    "        ASSERTION (always-true OR past-true): pick the bare entity.\n"
    "          The entity holds for this subject at some point in time.\n"
    "          A temporal qualifier (past tense, ex-, former-, previous,\n"
    "          history-of, post-) ASSERTS the entity in the past — the\n"
    "          subject DID undergo it. Pick the bare entity.\n"
    "        TRUE NEGATION (always-false): reply NONE. The raw denies\n"
    "          the entity ever held for this subject — natural-language\n"
    "          negation that scopes over the whole predicate (never had,\n"
    "          no, none, without, absent, denies, non-, un- when it\n"
    "          negates, etc.). This encodes the negative arm of a\n"
    "          contrast — semantically DISTINCT from the bare entity\n"
    "          AND from any treatment / agent / process that addresses\n"
    "          the entity. A negated raw is a separate class (no-\n"
    "          exposure / control), not the entity itself. Reply NONE\n"
    "          unless a candidate's preferred name itself denotes that\n"
    "          absence / no-exposure state.\n"
    "      The test is grammatical: ASSERTION → bare entity; TRUE\n"
    "      NEGATION → NONE. Applies universally across all columns.\n"
    "\n"
    "OUTPUT: reason briefly, then on the VERY LAST line output one of:\n"
    "  PICK: <integer>     (the chosen candidate index in [0..N-1])\n"
    "  PICK: NONE          (no candidate refers to the same entity)\n"
    "The parser requires the literal 'PICK:' prefix; without it the\n"
    "answer is treated as NONE.\n"
)


_VERIFIER_SYSTEM = (
    "You are a biomedical normalization verifier. Inputs (in this order,\n"
    "when present):\n"
    "  - study context: free-form text from the experiment's title /\n"
    "    summary / overall_design. Use it to decode short abbreviations\n"
    "    or brand names in the raw label (the abstract often defines\n"
    "    them explicitly).\n"
    "  - raw label, column, picked MeSH descriptor (with category and\n"
    "    scope).\n"
    "Decide whether the pick names the SAME biomedical entity as the\n"
    "raw — even when wording differs.\n"
    "\n"
    "DEFAULT BEHAVIOUR: KEEP. Reject ONLY when one of R1..R5 below is\n"
    "clearly true; in all other cases approve. When the study context\n"
    "spells out an abbreviation or brand name and the pick matches that\n"
    "expansion, KEEP — the context is authoritative for that study.\n"
    "\n"
    "Wording differences that still mean SAME entity (always KEEP):\n"
    "  • Abbreviation ↔ its standard biomedical expansion.\n"
    "  • Brand / trade name ↔ the drug's generic preferred name.\n"
    "  • Chemical / IUPAC / systematic name ↔ a drug's preferred name.\n"
    "  • Dose / concentration / duration suffix stripped from a\n"
    "    compound — the compound is still the entity.\n"
    "  • Adjectival organ form ↔ that organ's noun descriptor.\n"
    "  • '<organ> tissue' / '<organ> parenchyma' / '<organ> sample'\n"
    "    rolled up to the organ descriptor. The organ-name modifier\n"
    "    is what names the entity; the trailing histology word is\n"
    "    sample-source phrasing.\n"
    "  • Generic disease wording rolled up to its parent class when\n"
    "    the raw does NOT specify a histological subtype.\n"
    "  • Synonym / older nomenclature / minor spelling variant.\n"
    "\n"
    "REJECT only when one of these is clearly true:\n"
    "\n"
    "R1. WRONG COLUMN BRANCH — the pick's MeSH category is wrong for\n"
    "    the column. Tissue requires A (anatomy / cell / cell line);\n"
    "    Condition requires C or F (disease / mental disorder);\n"
    "    Treatment requires D or E (drug / biologic / procedure /\n"
    "    therapeutic technique). Cross-branch picks are NEVER valid.\n"
    "\n"
    "R2. DIFFERENT SPECIFIC ENTITY — the pick is a clearly different\n"
    "    drug, disease, gene, or anatomy that merely shares a word\n"
    "    with the raw (the picker confused two unrelated entities\n"
    "    because of literal token overlap).\n"
    "\n"
    "R3. INFORMATION-FREE RAW — the raw is an empty placeholder with\n"
    "    NO biomedical content (none, n/a, not specified). Such raws\n"
    "    cannot legitimately map to anything; REJECT regardless of\n"
    "    pick. NOTE: standard biomedical abbreviations are NOT\n"
    "    information-free — they encode one specific entity and must\n"
    "    be KEPT against their canonical expansion. Healthy / control /\n"
    "    normal / untreated / wild-type sample-state words for the\n"
    "    Condition column are also NOT information-free — they are\n"
    "    canonicalized BEFORE the picker runs and never reach the\n"
    "    verifier under the canonical surface form.\n"
    "\n"
    "R4. WRONG SPECIFICITY DIRECTION — the picker chose a more\n"
    "    specific subtype than the raw mentions (raw is generic,\n"
    "    pick is a histological subtype the raw never names), or\n"
    "    chose a generic-histology umbrella (e.g. a 'tissue' or\n"
    "    'parenchyma' descriptor) when the raw names a SPECIFIC\n"
    "    organ that has its own descriptor available. Same-level\n"
    "    same-entity picks are fine.\n"
    "\n"
    "R5. TRUE-NEGATION RAW. The raw asserts the ABSENCE of an entity,\n"
    "    not the entity itself. Read the raw and decide whether the\n"
    "    subject EVER possesses / undergoes the entity:\n"
    "      ASSERTION — the entity holds at some point (now or past).\n"
    "        Temporal qualifiers (past tense, ex-, former-, previous,\n"
    "        history-of, post-, current, recent) ASSERT the entity. A\n"
    "        bare-entity pick is fine — KEEP it.\n"
    "      NEGATION — the raw denies the entity ever held for this\n"
    "        subject. Natural-language negation scoping over the whole\n"
    "        predicate (never had, no, none, without, absent, non-, un-\n"
    "        when it negates, denies, etc.). Absence is its own class —\n"
    "        DIFFERENT from the bare entity AND from any agent /\n"
    "        process that addresses it. REJECT a bare-entity pick OR a\n"
    "        related-intervention pick for a negated raw, UNLESS the\n"
    "        pick's preferred name itself denotes the absence /\n"
    "        no-exposure state.\n"
    "    Grammatical, column-universal.\n"
    "\n"
    "OUTPUT FORMAT (strict): produce ONE LINE PER ITEM in this exact\n"
    "order, each line filled with your judgement (no skipping, no\n"
    "merging), then the VERDICT. Before answering R5, you MUST answer\n"
    "the 'subject_undergoes_picked_entity' line. Read the raw,\n"
    "identify the SUBJECT (the sample / patient the raw describes),\n"
    "and ask literally: 'Does this subject ever undergo / possess /\n"
    "experience the picked entity?'. Picked entity is the descriptor\n"
    "named on the 'picked:' line above; ignore the descriptor's\n"
    "scope text for this step. Use temporal-inclusive reading: past,\n"
    "present, and history-of all count as YES. Only a true denial\n"
    "(never, none, no, without, non-, un- when negating, etc.)\n"
    "scoping over the entity yields NO.\n"
    "Then:\n"
    "  subject_undergoes_picked_entity: YES|NO\n"
    "  R1_wrong_branch:     YES|NO\n"
    "  R2_different_entity: YES|NO\n"
    "  R3_info_free_raw:    YES|NO\n"
    "  R4_wrong_specificity: YES|NO\n"
    "  R5_negated_raw:      YES|NO   (YES iff\n"
    "    subject_undergoes_picked_entity is NO AND the picked entity\n"
    "    is NOT itself an absence/cessation/withdrawal descriptor.)\n"
    "  VERDICT: KEEP   (when ALL five R-items are NO)\n"
    "         | REJECT (when ANY R-item is YES)\n"
)


_SHORTFORM_SYSTEM = (
    "A GEO metadata label is a bare abbreviation/acronym (e.g. 'AD', 'CRC',\n"
    "'MS', 'HSC'). Using ONLY the provided study context, expand it to the\n"
    "full biomedical term it stands for IN THIS STUDY, in standard form\n"
    "(a disease, tissue/cell-type, or treatment name, as fits the field).\n"
    "If it is not an expandable abbreviation for a real biomedical entity\n"
    "(e.g. a stage/grade/sample code like 'T1' or 'G3', a group label, or\n"
    "the context does not tell you), reply exactly KEEP. Never guess a\n"
    "meaning the context does not support."
)


def _shortform_user(label: str, col: str, context: str) -> str:
    """User turn for the acronym fallback. The trailing cue that used to end
    this prompt ('...reply KEEP.\\nTERM:') invited the model to continue rather
    than to answer, while the parser searched the ANSWER for a tag the prompt
    never requested. The contract now lives in the system prompt, so the cue is
    gone and the two cannot drift apart."""
    return (
        f"{_format_context_block(context)}"
        f"Field: {col}\n"
        f"Abbreviation: {label}\n\n"
        f"Expand it to the full biomedical term for THIS study, or answer KEEP."
    )


def _is_shortform(v: str) -> bool:
    """A bare acronym that cannot be normalized without knowing the study
    (e.g. 'AD', 'CRC', 'MS'). Shape-only detection; the LLM decides whether it
    is actually expandable given context."""
    v = (v or "").strip()
    if not v or " " in v or ";" in v:
        return False
    if not (2 <= len(v) <= 5):
        return False
    if not v[0].isalpha() or not all(c.isalnum() for c in v):
        return False

    return any(c.isupper() for c in v) or any(c.isdigit() for c in v)


_CELLLINE_SYSTEM = (
    "You decide whether a raw GEO Tissue label names an established,\n"
    "immortalized CELL LINE, and if so you extract its bare identifier.\n"
    "\n"
    "What makes something a cell line (recognize it from biomedical\n"
    "knowledge — there is NO list to match against):\n"
    "  • It is a continuously-propagated, immortalized laboratory culture\n"
    "    with its OWN proper name or catalog code — a specific established\n"
    "    line, not a category. Such a name typically looks like a short\n"
    "    alphanumeric code or a coined laboratory designation that you\n"
    "    recognize as the name of one particular line, not an English\n"
    "    word for an organ, cell type, or disease.\n"
    "  • It is NOT a primary tissue, organ, whole-organism / in-vivo\n"
    "    sample, body fluid, or a generic cell type defined only by\n"
    "    lineage or marker (those name a category of cells, not one\n"
    "    specific immortalized line).\n"
    "\n"
    "A cell line is an IDENTITY, not a category: it must NOT be collapsed\n"
    "to a broader descriptor — doing so destroys the only thing that\n"
    "matters, namely WHICH line it is. So a cell line is never normalized;\n"
    "the bare identifier is the answer.\n"
    "\n"
    "The label usually wraps the line's identifier in descriptive words —\n"
    "the host species, the tissue or disease of origin, and phrases such\n"
    "as 'cell line', 'cells', 'derived', 'subclone'. Your job is to\n"
    "DELETE those descriptive wrapper words and return ONLY the bare\n"
    "identifier, copied VERBATIM from the raw (same characters, case,\n"
    "hyphens, digits). Pattern (placeholders, not real names):\n"
    "  • '<species> <disease> cell line <ID>'  -> <ID>\n"
    "  • '<ID> <tissue> cancer cells'           -> <ID>\n"
    "  • '<ID>'  (already just the identifier)  -> <ID>  (unchanged)\n"
    "Never translate, expand, canonicalize, or add words like 'Cells';\n"
    "never output anything not literally present in the raw.\n"
    "\n"
    "Rules:\n"
    "  • Raw IS or CONTAINS an established cell line -> output its bare\n"
    "    identifier exactly as it appears in the raw.\n"
    "  • Raw is a primary tissue / organ / in-vivo sample / body fluid /\n"
    "    generic cell type with NO specific line identifier -> output NO.\n"
    "  • Unsure whether a token is a genuine line identifier -> output NO.\n"
    "\n"
    "OUTPUT: reason briefly, then on the VERY LAST line output exactly one\n"
    "of:\n"
    "  LINE: <bare-identifier>   (a verbatim substring of the raw)\n"
    "  LINE: NO                  (not a cell line)\n"
)


class Phase2Mesh:
    """Collapse Phase 1 raw labels to canonical MeSH names (or mint new
    out-of-distribution (OOV) mesh entries when no MeSH descriptor fits)."""

    def __init__(
        self,
        db: MeshDB | None = None,
        *,
        top_k: int = _TOP_K,
        use_episodic: bool = True,
        use_picker: bool = True,
        use_pubtator: bool = True,
        use_verifier: bool = True,
        use_polarity: bool = False,
        pubtator_sleep: float = 0.34,
        cache: Any = None,
    ):
        """``cache`` is an optional ``GSEContextCache`` used for per-GSE
        canonical sibling-consistency. When provided AND ``gse_id`` is
        passed to ``collapse`` / ``collapse_record``, every sibling raw
        in the same experiment routes through ``gse_phase2_canon`` first
        (Tier 1.5 / 1.6) and falls back to the global cascade only on
        miss. Persisted decisions ensure two equivalent raws within one
        GSE collapse to the SAME canonical id.

        ``use_polarity`` toggles the Tier 0.5 negation short-circuit.
        Default on; turn off only for ablation studies.
        """
        self.db = db or MeshDB()
        self.top_k = top_k
        self.use_episodic = use_episodic
        self.use_picker = use_picker
        self.use_verifier = use_verifier
        self.use_polarity = use_polarity
        self.use_pubtator = use_pubtator and PubTatorNormalizer is not None
        self._pt = (
            PubTatorNormalizer(sleep=pubtator_sleep) if self.use_pubtator else None
        )
        self._cache = cache

    def collapse(
        self, raw: str, col: str, context: str = "", gse_id: str | None = None
    ) -> dict:
        """Resolve one (Phase 1 raw, col) pair to canonical name(s).

        ``context`` is optional GSE-level free-form text (title + summary +
        overall_design from the source experiment). When provided, it is
        passed to the LLM picker / verifier so abbreviations and
        brand names defined in the experiment metadata can be expanded
        (e.g. an AD/PD/NSCLC abbreviation that the abstract spells out).

        ``gse_id`` opts into per-GSE sibling consistency: when set AND a
        ``cache`` was passed to ``__init__``, equivalent siblings within
        the same experiment collapse to the same canonical id without
        re-running the LLM cascade.
        """
        if col not in COL_CATS:
            raise ValueError(f"col must be one of {tuple(COL_CATS)}, got {col !r}")
        raw = (raw or "").strip()
        if not raw or raw.lower() == NS.lower():
            return {"label": NS, "components": [], "id": ""}

        sep = r";\s*|\s+\+\s+" if col == "Treatment" else r";\s*"
        parts = [p.strip() for p in re.split(sep, raw) if p.strip()]
        comps: list[dict] = []

        _multi = len(parts) > 1
        for p in parts:
            comps.append(
                self._resolve_one(p, col, context, gse_id, allow_sibling=not _multi)
            )

        _DELIBERATE = ("negation_short_circuit",)
        for _c, _src in zip(comps, parts):
            if str(_c.get("source") or "") in _DELIBERATE:
                continue
            if (
                str(_c.get("name") or "").strip().lower() in ("", NS.lower())
                and _src.strip()
            ):
                _c["name"] = _src.strip()
                _c["id"] = ""
                _c["oov"] = True
        canonical_names = [c["name"] for c in comps]
        canonical_ids = [c["id"] for c in comps]
        return {
            "label": "; ".join(canonical_names),
            "id": "; ".join(canonical_ids),
            "components": comps,
        }

    def collapse_record(self, record: dict) -> dict:
        """Apply collapse() to all three label cols of a Phase 1 record.
        Output row contains the resolved canonical NAMES under the same
        keys; the MeSH / OOV-mesh IDs are kept under ``<col>_id`` for audit.

        ``record`` may contain a ``gse_context`` field (free-form text from
        title/summary/overall_design); when present it is forwarded to the
        picker/verifier so study-defined abbreviations can be expanded.
        """
        out = dict(record)
        ctx = (record.get("gse_context") or "").strip()
        gse_id = (
            record.get("gse") or record.get("series_id") or record.get("gse_id") or None
        )
        for col in LABEL_COLS:
            res = self.collapse(record.get(col, NS), col, ctx, gse_id)
            out[col] = res["label"]
            out[f"{col}_id"] = res["id"]
            out[f"{col}_components"] = res["components"]

        out["Treatment_dose"] = _extract_dose(record.get("Treatment", "") or "")

        staged = {
            "phase1b": {c: record.get(c, NS) for c in LABEL_COLS},
            "phase2": {c: out.get(c, NS) for c in LABEL_COLS},
            **{k: record.get(k, "") for k in _RAW_FIELDS},
        }
        apply_treatment_grounding(staged)
        apply_condition_cellline_guard(staged)
        for c in LABEL_COLS:
            out[c] = staged["phase2"].get(c, out.get(c, NS))
        return out

    def _resolve_one(
        self,
        label: str,
        col: str,
        context: str = "",
        gse_id: str | None = None,
        allow_sibling: bool = True,
    ) -> dict:

        if col == "Condition":
            canon = _CONDITION_CONTROL_CANONICAL.get(label.strip().lower())
            if canon is not None:
                return {
                    "raw": label,
                    "id": "",
                    "name": canon,
                    "source": "condition_control_canonical",
                }

        if col == "Tissue" and label.strip().lower() in _TISSUE_GENERIC_PLACEHOLDERS:
            return {
                "raw": label,
                "id": "",
                "name": NS,
                "source": "tissue_generic_placeholder",
            }

        if self.use_polarity:

            if not (
                self.db.lookup_mesh(label, col) or _SUBTYPE_NON_RE.match(label.strip())
            ):
                polarity = self._classify_polarity(label, col, context)
                if polarity == "NEGATE":
                    return {
                        "raw": label,
                        "id": "",
                        "name": NS,
                        "source": "negation_short_circuit",
                    }

        if allow_sibling and gse_id and self._cache is not None:
            raw_lc = label.strip().lower()
            hit = self._cache.get_gse_canon(gse_id, col, raw_lc)
            if hit:
                return {
                    "raw": label,
                    "id": hit["canon_id"],
                    "name": hit["canon_name"],
                    "source": f"gse_canon_exact(n={hit['n_uses']})",
                }

            aug_q = _augmented_query(label)
            if aug_q:
                hit = self._cache.get_gse_canon(gse_id, col, aug_q)
                if hit:
                    self._cache.set_gse_canon(
                        gse_id, col, raw_lc, hit["canon_id"], hit["canon_name"]
                    )
                    return {
                        "raw": label,
                        "id": hit["canon_id"],
                        "name": hit["canon_name"],
                        "source": "gse_canon_morph",
                    }

            raw_aug = _augment_raw_tokens(label)

            _NEG_TOKENS = {
                "no",
                "not",
                "never",
                "absent",
                "negative",
                "denied",
                "without",
                "unaffected",
                "neg",
                "false",
                "non",
            }
            if raw_aug:
                raw_neg = bool(raw_aug & _NEG_TOKENS)
                for entry in self._cache.list_gse_canon(gse_id, col):
                    sib_aug = _augment_raw_tokens(entry["raw_lc"])
                    if not sib_aug:
                        continue

                    if sib_aug <= raw_aug or raw_aug <= sib_aug:
                        sib_neg = bool(sib_aug & _NEG_TOKENS)
                        if raw_neg != sib_neg:
                            continue
                        self._cache.set_gse_canon(
                            gse_id, col, raw_lc, entry["canon_id"], entry["canon_name"]
                        )
                        return {
                            "raw": label,
                            "id": entry["canon_id"],
                            "name": entry["canon_name"],
                            "source": "gse_canon_overlap",
                        }

        result = self._resolve_one_global(label, col, context)

        if (
            allow_sibling
            and gse_id
            and self._cache is not None
            and result.get("id")
            and result.get("name")
        ):
            self._cache.set_gse_canon(gse_id, col, label, result["id"], result["name"])
        return result

    def _expansion_spells(self, acronym: str, resolved: dict) -> bool:
        """Does the concept an expansion landed on actually spell the acronym?

        The deterministic branch quotes its expansion out of the study's own
        words, so it cannot invent one. The model branch has no such floor, and
        its characteristic failure is fluent rather than noisy: given a study
        whose every sentence is about Parkinson's disease it expands HD to
        Parkinson Disease, and given an arthritis study it expands OA to
        Arthritis, Rheumatoid. Both are the study's SUBJECT, not the
        abbreviation's meaning, and both arrive with the same confidence as a
        correct answer -- nothing downstream can separate them afterwards.

        Checking at concept level rather than on the returned string is what
        makes the test safe: MeSH headings frequently do not spell their own
        abbreviation, and testing the heading alone would reject six of the 39
        curated acronyms. Their synonym lists do spell them, so every one of the
        39 passes while Parkinson-for-HD still fails.

        A rejected expansion is not replaced by a second guess. The value falls
        through to the ordinary cascade and ends out-of-vocabulary, which is the
        honest report: this acronym was not resolved.
        """
        cid = (resolved or {}).get("id") or ""
        if not cid:
            return True
        forms = self.db.forms_of(cid) or [resolved.get("name") or ""]
        return any(acronym_expand.supports_acronym(acronym, f) for f in forms)

    def _expand_shortform(self, label: str, col: str, context: str) -> str | None:
        """Expand a bare acronym to its full biomedical term, or None to keep it
        verbatim. The expansion then re-enters the cascade, so spelling variants
        ('leukaemia') are handled by retrieval exactly as any other value.

        Evidence is scoped to the experiment the sample belongs to, because an
        acronym belongs to its experiment: HD is Huntington disease in one study
        and healthy donors in the next, and only that study's own material can
        say which.

          1. the study's OWN definition, quoted rather than inferred;
          2. otherwise the model, reading the same study context plus the
             metadata of the samples that actually carry the value.

        The released stage had only (2), and it never fired: its system prompt
        did not ask for the ``TERM:`` tag that its parser required, so every
        well-formed answer parsed to None, and 2 of 39 acronyms were expanded --
        both of them by other paths entirely.
        """
        # 1. This study's own words. A definition quoted from the GSE that the
        #    sample belongs to cannot be wrong about that GSE, and costs no
        #    model call.
        own = acronym_expand.find_definition(label, context)
        if own:
            return own

        # 2. Otherwise the model, given the SAME study context. There is
        #    deliberately no corpus-wide dictionary here: an acronym belongs to
        #    its experiment, and a meaning pooled from other studies would
        #    overwrite a local one. A study writing HD for healthy donors and
        #    never mentioning Huntington must not inherit Huntington disease
        #    because twelve unrelated studies used HD that way.
        text = self._call_lm(acronym_expand.SHORTFORM_SYSTEM,
                             _shortform_user(label, col, context))
        return acronym_expand.parse_expansion(text, label)

    def _resolve_one_global(self, label: str, col: str, context: str = "") -> dict:
        # Tier 0: no-answer guard. Retrieval always returns a neighbour and the
        # picker is never offered "none of these", so a value that MEASURES a
        # finding rather than naming one gets recorded as a finding. Placed
        # ahead of every other tier -- including episodic replay, which would
        # otherwise keep serving a wrong answer already in the cache -- because
        # having no controlled-vocabulary answer is a property of the value and
        # must not depend on which tier happens to fire first.
        if is_pure_qualifier(label):
            minted = (self.db.lookup_oov_mesh(label, col)
                      or self.db.create_oov_mesh(label, col))
            return {"raw": label, "id": minted["id"], "name": minted["label"],
                    "source": "oov:no-vocabulary-answer"}

        # Episodic replay is keyed on the LABEL alone, so it can only be valid
        # for a label that means the same thing everywhere. A bare acronym is
        # the one kind of label for which that is never true: HD is Huntington
        # disease in one study and healthy donors in the next, and replaying
        # whichever meaning was recorded first silently overrides the study that
        # is actually being resolved.
        #
        # It also replays the previous run's MISTAKES. In this corpus the cache
        # holds NSCLC -> ART-C-00017, the out-of-vocabulary cluster it was
        # wrongly minted into; served from here it returns before the expansion
        # stage is ever reached, which is why the acronym never recovered no
        # matter what that stage did. Study-scoped labels therefore skip the
        # cache and are resolved against their own study every time.
        if self.use_episodic and not _is_shortform(label):
            history = self.db.get_resolution_history(label, col, k=1)
            if history:
                h = history[0]
                return {
                    "raw": label,
                    "id": h["output_id"],
                    "name": h["output_name"],
                    "source": f"episodic:{h['source']}",
                }

        if col == "Tissue" and CellLineDB is not None:
            cl = CellLineDB.get().match_ref(label)
            if cl:
                cvcl, primary = cl
                if cvcl:

                    self.db.record_resolution(
                        label, col, cvcl, primary, "cell-line-ref"
                    )
                    return {
                        "raw": label,
                        "id": cvcl,
                        "name": primary,
                        "source": "cell-line-ref",
                    }

                ood = self.db.lookup_oov_mesh(primary, col) or self.db.create_oov_mesh(
                    primary, col
                )
                self.db.record_resolution(
                    label, col, ood["id"], ood["label"], "cell-line-ref"
                )
                return {
                    "raw": label,
                    "id": ood["id"],
                    "name": ood["label"],
                    "source": "cell-line-ref",
                }

        if col == "Tissue" and self.use_picker:
            line_id = self._cellline_id(label, context)
            if line_id:

                cl = (
                    CellLineDB.get().match_ref(line_id)
                    if CellLineDB is not None
                    else None
                )
                if cl and cl[0]:
                    cvcl, primary = cl
                    self.db.record_resolution(label, col, cvcl, primary, "cell-line-id")
                    return {
                        "raw": label,
                        "id": cvcl,
                        "name": primary,
                        "source": "cell-line-id",
                    }
                ood = self.db.lookup_oov_mesh(line_id, col) or self.db.create_oov_mesh(
                    line_id, col
                )
                self.db.record_resolution(
                    label, col, ood["id"], ood["label"], "cell-line-id"
                )
                return {
                    "raw": label,
                    "id": ood["id"],
                    "name": ood["label"],
                    "source": "cell-line-id",
                }

        if self.use_picker and context and _is_shortform(label):
            expanded = self._expand_shortform(label, col, context)
            if expanded and expanded.strip().lower() != label.strip().lower():
                out = self._resolve_one_global(expanded, col, context)
                if self._expansion_spells(label, out):
                    return {
                        **out,
                        "raw": label,
                        "source": f"shortform:{out.get('source','')}",
                    }

        mesh_hits = self.db.lookup_mesh(label, col)
        if len(mesh_hits) == 1:
            return self._finalize_mesh(label, col, mesh_hits[0], "mesh-exact")
        if len(mesh_hits) > 1 and self.use_picker:
            chosen = self._pick(label, col, mesh_hits, context)
            if chosen is not None and self._verify_pick(label, col, chosen, context):
                return self._finalize_mesh(label, col, chosen, "mesh-exact-picked")

        ood = self.db.lookup_oov_mesh(label, col)
        if ood:
            self.db.record_resolution(
                label, col, ood["id"], ood["label"], "ood-mesh-existing"
            )
            return {
                "raw": label,
                "id": ood["id"],
                "name": ood["label"],
                "source": "ood-mesh-existing",
            }

        if self.use_picker:
            cands: list[dict] = []
            seen_ids: set[str] = set()

            pt_cand = self._pt_candidate(label, col)
            if pt_cand and pt_cand["id"] not in seen_ids:
                cands.append(pt_cand)
                seen_ids.add(pt_cand["id"])

            for c in self.db.find_similar_mesh(label, col, k=self.top_k):
                if c["id"] in seen_ids:
                    continue
                cands.append(c)
                seen_ids.add(c["id"])

            aug_q = _augmented_query(label)
            if aug_q:

                for h in self.db.lookup_mesh(aug_q, col):
                    if h["id"] in seen_ids:
                        continue
                    h = {**h, "score": max(float(h.get("score") or 0.0), 0.95)}
                    cands.append(h)
                    seen_ids.add(h["id"])

                for c in self.db.find_similar_mesh(aug_q, col, k=self.top_k):
                    if c["id"] in seen_ids:
                        continue
                    cands.append(c)
                    seen_ids.add(c["id"])

            if cands:
                chosen = self._pick(label, col, cands, context)
                if chosen is not None and self._verify_pick(
                    label, col, chosen, context
                ):
                    src = "mesh-pubtator" if chosen is pt_cand else "mesh-semantic"
                    return self._finalize_mesh(label, col, chosen, src)

        if col == "Treatment" and self.use_picker:
            stripped = _strip_dose(label)
            if stripped and stripped.lower() != label.lower():

                bare_hits = self.db.lookup_mesh(stripped, col)
                if len(bare_hits) == 1:
                    return self._finalize_mesh(
                        label, col, bare_hits[0], "mesh-dose-stripped-exact"
                    )
                if len(bare_hits) > 1:
                    chosen = self._pick(stripped, col, bare_hits, context)
                    if chosen is not None and self._verify_pick(
                        stripped, col, chosen, context
                    ):
                        return self._finalize_mesh(
                            label, col, chosen, "mesh-dose-stripped-picked"
                        )

                bare_cands: list[dict] = []
                bare_seen: set[str] = set()
                pt2 = self._pt_candidate(stripped, col)
                if pt2:
                    bare_cands.append(pt2)
                    bare_seen.add(pt2["id"])
                for c in self.db.find_similar_mesh(stripped, col, k=self.top_k):
                    if c["id"] in bare_seen:
                        continue
                    bare_cands.append(c)
                    bare_seen.add(c["id"])
                if bare_cands:
                    chosen = self._pick(stripped, col, bare_cands, context)
                    if chosen is not None and self._verify_pick(
                        stripped, col, chosen, context
                    ):
                        src = (
                            "mesh-dose-stripped-pubtator"
                            if chosen is pt2
                            else "mesh-dose-stripped-semantic"
                        )
                        return self._finalize_mesh(label, col, chosen, src)

        minted = self.db.create_oov_mesh(label, col)
        self.db.record_resolution(
            label, col, minted["id"], minted["label"], "ood-mesh-minted"
        )
        return {
            "raw": label,
            "id": minted["id"],
            "name": minted["label"],
            "source": "ood-mesh-minted",
        }

    def _pt_candidate(self, label: str, col: str) -> dict | None:
        """Run PubTator3 normalize and return one MeSH candidate (looked up
        in the local mesh.sqlite to enrich with name/scope/category) or None.

        We only emit the candidate when PT returns a MeSH ID that exists
        in our local MeSH DB AND whose category passes the col gate. PT
        misses (returns empty id) and out-of-category hits are dropped —
        the LLM picker only sees high-precision MeSH candidates.
        """
        if not self.use_pubtator or self._pt is None:
            return None
        try:
            r = self._pt.normalize(label, col)
        except Exception:
            return None
        mid = (r or {}).get("id") or ""
        if not mid:
            return None
        row = self.db.con.execute(
            "SELECT id, name, category, scope FROM mesh_terms WHERE id = ?",
            (mid,),
        ).fetchone()
        if not row:
            return None
        cats = COL_CATS.get(col, ())
        if cats and row["category"] not in cats:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "category": row["category"],
            "scope": row["scope"] or "",
            "score": 1.0,
        }

    def _finalize_mesh(self, label: str, col: str, hit: dict, source: str) -> dict:
        self.db.record_resolution(label, col, hit["id"], hit["name"], source)
        return {"raw": label, "id": hit["id"], "name": hit["name"], "source": source}

    def _cellline_id(self, label: str, context: str = "") -> str | None:
        """If the Tissue raw names an established cell line, return its
        bare identifier (descriptive wrapper words deleted), copied
        verbatim from the raw; otherwise None.

        The LLM both decides "is this a cell line?" and extracts the bare
        identifier by deleting wrapper words. A verbatim-substring guard
        enforces the deletion-only contract: the returned id MUST occur in
        the raw, so the model can never invent or canonicalize a name (no
        MeSH mapping, no hardcoded line list).
        """
        ctx_block = _format_context_block(context)
        user = (
            f"{ctx_block}"
            f"raw Tissue label: {label}\n\n"
            f"Is this a named cell line? If so, delete the descriptive\n"
            f"wrapper words and output the bare identifier verbatim.\n"
            f"LINE:"
        )
        text = self._call_lm(_CELLLINE_SYSTEM, user)
        ms = re.findall(
            r"LINE\s*:\s*(\S.*?)\s*$", text, flags=re.IGNORECASE | re.MULTILINE
        )
        if not ms:
            return None
        cand = ms[-1].strip().strip(".\"'")
        if not cand or cand.upper() == "NO":
            return None

        if cand.lower() not in label.lower():
            return None
        return cand

    def _pick(
        self, label: str, col: str, candidates: list[dict], context: str = ""
    ) -> dict | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        raw_aug = _augment_raw_tokens(label)

        def _rerank_key(c: dict) -> tuple:
            name_toks = _candidate_name_tokens(c)
            inter = raw_aug & name_toks

            coverage = len(inter) / max(1, len(name_toks))
            if not inter:
                band = 0
            elif coverage >= 1.0:
                band = 3
            elif coverage >= 0.5:
                band = 2
            else:
                band = 1
            return (-band, -float(c.get("score") or 0.0))

        candidates = sorted(candidates, key=_rerank_key)

        if candidates:
            top = candidates[0]
            top_toks = _candidate_name_tokens(top)
            if not (raw_aug & top_toks) and float(top.get("score") or 0.0) < 0.95:
                return None

        if candidates and raw_aug:
            top = candidates[0]
            top_toks = _candidate_name_tokens(top)
            if (
                top_toks
                and len(top_toks) == 1
                and top_toks.issubset(raw_aug)
                and float(top.get("score") or 0.0) >= 0.45
            ):
                return top

        candidates = candidates[:12]

        lines = []
        for i, c in enumerate(candidates):
            scope = (c.get("scope") or "").strip().replace("\n", " ")
            if len(scope) > 220:
                scope = scope[:220].rsplit(" ", 1)[0] + "…"
            cat = f"[{c.get('category','?')}]"
            lines.append(
                f"{i}. {c['name']} {cat}" + (f" — {scope}" if scope else "")
            )
        ctx_block = _format_context_block(context)
        user = (
            f"{ctx_block}"
            f"raw label: {label}\n"
            f"column: {col}\n"
            f"candidates:\n  " + "\n  ".join(lines) + "\n\n"
            f"Pick:"
        )
        text = self._call_lm(_PICKER_SYSTEM, user).strip()

        m = re.search(r"PICK\s*:\s*(\d{1,2}|NONE)\b", text, flags=re.IGNORECASE)
        tok: str | None = m.group(1).upper() if m else None
        if tok is None:
            ms = re.findall(r"\b(\d{1,2}|NONE)\b", text, flags=re.IGNORECASE)
            tok = ms[-1].upper() if ms else None
        if tok is None or tok == "NONE":
            return None
        try:
            i = int(tok)
        except ValueError:
            return None
        if 0 <= i < len(candidates):
            return candidates[i]
        return None

    def _classify_polarity(self, label: str, col: str, context: str = "") -> str:
        """Return 'ASSERT' or 'NEGATE' for ``label`` in ``col`` context.

        Cached per (raw_lc, col, prompt_version) in
        ``polarity_decisions`` — first agent in the fleet to see a
        novel surface form pays the LLM call; every subsequent agent
        reads the cached verdict. On parse failure, defaults to
        ASSERT (conservative — keep the existing cascade).

        When ``context`` is provided AND the raw label contains a
        coded value (digit / paren-code / colon-code), the context is
        injected so an inline legend (e.g. '<field> (0,1,2 = level-A,
        level-B, level-C): X') can decode the code before
        classification. Coded raws are NOT cached — the same coded
        surface can encode different meanings in different studies;
        context-decoded verdicts are computed per call. Plain-prose
        raws are cached cluster-wide as before.
        """
        raw_lc = label.strip().lower()
        if not raw_lc:
            return "ASSERT"

        is_coded = bool(
            re.search(r"[\(:]\s*\d", raw_lc) or re.fullmatch(r"\d+(\.\d+)?", raw_lc)
        )

        if not is_coded:
            cached = self.db.get_polarity(raw_lc, col, _PROMPT_VERSION)
            if cached is not None:
                return cached

        ctx_block = _format_context_block(context) if (is_coded and context) else ""
        user = f"{ctx_block}raw label: {label}\ncolumn: {col}\n\nClassify polarity:"
        text = self._call_lm(_POLARITY_SYSTEM, user)
        m = re.search(r"POLARITY\s*:\s*(ASSERT|NEGATE)\b", text, flags=re.IGNORECASE)
        polarity = m.group(1).upper() if m else "ASSERT"
        if not is_coded:
            self.db.cache_polarity(raw_lc, col, _PROMPT_VERSION, polarity)
        return polarity

    def _verify_pick(
        self, label: str, col: str, candidate: dict, context: str = ""
    ) -> bool:
        """Self-check the picker's choice. Returns True to KEEP, False
        to REJECT (in which case the caller falls through to the next
        tier and ultimately mints an OOV-mesh entry). On parse failure, returns
        False (conservative).

        Cluster-scale path: consult the shared ``verifier_decisions``
        cache first — any agent in the fleet that already verified this
        ``(raw, col, picked_id)`` under the current prompt version saves
        every other agent the LLM call. Only on cache miss does the
        verifier LLM fire; the verdict is then written through for
        future fleet-wide reuse.
        """
        if not self.use_verifier:
            return True

        raw_lc = label.strip().lower()
        picked_id = candidate.get("id", "")
        if picked_id:
            cached = self.db.get_verifier_verdict(
                raw_lc,
                col,
                picked_id,
                _PROMPT_VERSION,
            )
            if cached is not None:
                return cached == "KEEP"

        scope = (candidate.get("scope") or "").strip().replace("\n", " ")
        if len(scope) > 280:
            scope = scope[:280].rsplit(" ", 1)[0] + "…"
        cat = candidate.get("category", "?")
        ctx_block = _format_context_block(context)
        user = (
            f"{ctx_block}"
            f"raw label: {label}\n"
            f"column: {col}\n"
            f"picked: {candidate['name']} [{cat}]\n"
            f"scope: {scope or '(no scope note)'}\n\n"
            f"Run R1..R5 then output the VERDICT line."
        )
        text = self._call_lm(_VERIFIER_SYSTEM, user)
        m = re.search(r"VERDICT\s*:\s*(KEEP|REJECT)", text, flags=re.IGNORECASE)
        verdict = m.group(1).upper() if m else "REJECT"
        if picked_id:
            self.db.cache_verifier_verdict(
                raw_lc,
                col,
                picked_id,
                _PROMPT_VERSION,
                verdict,
            )
        return verdict == "KEEP"

    def promote_global_canons(self, min_gses: int = 3) -> int:
        """Mirror unanimous per-GSE canonical decisions into the global
        episodic table. Call periodically (e.g. at end of a pipeline run)
        so future first-time studies see cluster-wide consensus on
        Tier 1 (episodic) and skip the LLM cascade entirely.

        Returns the number of (col, raw_lc) entries promoted.

        Safe — ``list_promote_candidates`` already drops any (col, raw_lc)
        with disagreement across GSEs. Idempotent: re-running just
        refreshes ``record_resolution`` rows in place.
        """
        if self._cache is None:
            return 0
        cands = self._cache.list_promote_candidates(min_gses=min_gses)
        n = 0
        for c in cands:
            try:
                self.db.record_resolution(
                    c["raw_lc"],
                    c["col"],
                    c["canon_id"],
                    c["canon_name"],
                    f"global-promote(n_gses={c['n_gses']})",
                )
                n += 1
            except Exception as e:
                print(
                    f"[phase2_mesh] promote skipped {c['raw_lc']!r}: {e !r}",
                    flush=True,
                )
        return n

    @staticmethod
    def _call_lm(system: str, user: str) -> str:

        if os.environ.get("LLM_BACKEND", "ollama").lower() in (
            "vllm",
            "sglang",
            "openai",
        ):
            from llm_backend import chat as _vllm_chat

            return _vllm_chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                model=_LLM_MODEL,
                temperature=0.0,
                seed=0,
                num_predict=int(os.environ.get("PHASE2_NUM_PREDICT", "768")),
                num_ctx=_LLM_NUM_CTX,
                timeout=180,
                retries=3,
            )

        if _requests is None:
            raise RuntimeError("phase2_mesh: `requests` not installed")

        _think = os.environ.get("PHASE2_THINK", "true").lower() in ("1", "true", "yes")
        body = {
            "model": _LLM_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "think": _think,
            "keep_alive": -1,
            "options": {
                "temperature": 0.0,
                "seed": 0,
                "num_predict": 2048 if _think else 512,
                "num_ctx": _LLM_NUM_CTX,
            },
        }
        attempt = 0
        while True:
            try:
                r = _requests.post(
                    _OLLAMA_URL.rstrip("/") + "/api/chat", json=body, timeout=180
                )
                r.raise_for_status()
                break
            except _requests.exceptions.RequestException as _e:

                _sc = getattr(getattr(_e, "response", None), "status_code", None)
                if _sc is not None and 400 <= _sc < 500 and _sc not in (408, 429):
                    print(f"[p2 FATAL] permanent HTTP {_sc}: {_e !r}", flush=True)
                    raise
                attempt += 1
                if attempt % 20 == 0:
                    print(
                        f"[p2 retry] {attempt} transient retries; last={_e !r}",
                        flush=True,
                    )
                time.sleep(min(60.0, 2.0 * attempt))
        data: Any = r.json()
        msg = data.get("message", {}) or {}

        return (msg.get("content") or "") + "\n" + (msg.get("thinking") or "")


__all__ = ["Phase2Mesh", "NS", "LABEL_COLS"]


_RAW_FIELDS = (
    "characteristics",
    "source",
    "title",
    "treatment_protocol",
    "description",
    "characteristics_ch1",
    "source_name_ch1",
    "treatment_protocol_ch1",
)


_STOP = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "были",
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
    "na",
    "day",
    "days",
    "hour",
    "hours",
    "hr",
    "min",
    "hours",
    "week",
    "weeks",
    "dose",
    "doses",
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
    "kg",
    "percent",
    "conc",
    "concentration",
    "stimulation",
    "exposure",
    "time",
    "point",
}
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")
_NUM = re.compile(r"\d")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower())


def _raw_blob(sample: dict) -> str:
    return _norm(" ".join(str(sample.get(k) or "") for k in _RAW_FIELDS))


def _content_tokens(value: str) -> list[str]:
    """Grounding-relevant tokens of a treatment span: drug/gene names, alnum
    codes. Drops pure controls, dose units, and filler."""
    toks = []
    for t in _WORD.findall(_norm(value)):
        if t in _STOP:
            continue
        toks.append(t)

    for t in re.findall(r"[a-z]*\d[a-z0-9]*", _norm(value)):
        if len(t) >= 3 and re.search(r"[a-z]", t):
            toks.append(t)
    return toks


def _tokens_in_raw(raw_set: set, raw: str, raw_stripped: str, value: str) -> bool:
    """True if ANY content token of `value` appears in the raw text. Checks a
    separator-stripped copy too, so abbreviations survive punctuation differences
    (IL6 vs IL-6, TGFb vs TGF-b)."""
    for t in _content_tokens(value):
        if t in raw_set or t in raw:
            return True
        ts = re.sub(r"[^a-z0-9]", "", t)
        if len(ts) >= 3 and ts in raw_stripped:
            return True
    return False


def treatment_is_grounded(sample: dict, ph1c_val: str, ph2_val: str) -> bool:
    """Grounded if the VERBATIM (phase1b) treatment — or, as a fallback, the
    normalized (phase2) treatment — has any content token in this sample's own
    raw text. Anchoring on phase1b avoids false blanks from phase2 canonical
    renames (DMSO->Dimethyl Sulfoxide, 5-ASA->Mesalamine, IGF-1->Insulin-Like
    Growth Factor I). A value with no content tokens (pure control/vehicle/dose)
    counts as grounded."""
    if not _content_tokens(ph1c_val) and not _content_tokens(ph2_val):
        return True
    raw = _raw_blob(sample)
    raw_set = set(raw.split())
    raw_stripped = re.sub(r"[^a-z0-9]", "", raw)
    return _tokens_in_raw(raw_set, raw, raw_stripped, ph1c_val) or _tokens_in_raw(
        raw_set, raw, raw_stripped, ph2_val
    )


def apply_treatment_grounding(sample: dict) -> bool:
    """Blank phase1b+phase2 Treatment only when the WHOLE value is ungrounded in
    this sample's own raw text (a leaked-sibling drug or world-knowledge
    substitution). Conservative: whole-value, not per-component, to protect
    recall."""
    ph1c = sample.get("phase1b") or {}
    ph2 = sample.get("phase2") or {}
    ph1c_val = str(ph1c.get("Treatment") or "")
    ph2_val = str(ph2.get("Treatment") or "")
    active = [
        v
        for v in (ph1c_val, ph2_val)
        if v.strip() and v.strip().lower() not in ("not specified", "ns")
    ]
    if not active:
        return False
    if treatment_is_grounded(sample, ph1c_val, ph2_val):
        return False
    changed = False
    for d in (ph1c, ph2):
        if isinstance(d, dict) and str(
            d.get("Treatment") or ""
        ).strip().lower() not in ("not specified", "", "ns"):
            d["Treatment"] = "Not Specified"
            changed = True
    return changed


_DISEASE_HINT = re.compile(
    r"cancer|carcinoma|tumou?r|leukemia|leukaemia|lymphoma|sarcoma|melanoma|"
    r"glioma|glioblastoma|adenoma|neoplasm|syndrome|disease|disorder|itis\b|"
    r"emia\b|opathy|osis\b|infection|diabetes|fibrosis|healthy|normal|control",
    re.IGNORECASE,
)


def condition_is_bare_cellline(value: str) -> bool:
    """True when the whole Condition value is just a recognised cell line and
    carries no disease/phenotype hint (so it belongs in Tissue, not Condition)."""
    if CellLineDB is None:
        return False
    v = str(value or "").strip()
    if not v or _DISEASE_HINT.search(v):
        return False
    db = CellLineDB.get()

    for cand in (v, re.sub(r"\s+", "", v)):
        m = db.match(cand)
        if m and re.sub(r"\s+", "", m).lower() == re.sub(r"\s+", "", v).lower():
            return True
    return False


def _raw_has_disease(sample: dict) -> bool:
    raw = " ".join(str(sample.get(k) or "") for k in _RAW_FIELDS)
    return bool(_DISEASE_HINT.search(raw))


def condition_cellline_status(sample: dict) -> str:
    """Classify the phase2 Condition cell-line situation:
    'clean'   — bare cell line, no disease anywhere in raw  -> safe to blank
    'recover' — bare cell line BUT a disease term is present in raw -> the
                disease is recoverable; a blank would lose it. Flag for a
                targeted Condition re-extraction rather than blanking.
    'ok'      — not a bare-cell-line condition.
    """
    val = (sample.get("phase2") or {}).get("Condition")
    if not val or not condition_is_bare_cellline(val):
        return "ok"
    return "recover" if _raw_has_disease(sample) else "clean"


def apply_condition_cellline_guard(
    sample: dict, blank_recoverable: bool = False, phases=("phase1b", "phase2")
) -> bool:
    """Blank a bare-cell-line Condition. By default (conservative) blanks ONLY
    the 'clean' case (no disease in raw); the 'recover' case is left intact for a
    targeted re-extraction unless blank_recoverable=True."""
    st = condition_cellline_status(sample)
    if st == "ok" or (st == "recover" and not blank_recoverable):
        return False
    changed = False
    for ph in phases:
        d = sample.get(ph)
        if (
            isinstance(d, dict)
            and d.get("Condition")
            and condition_is_bare_cellline(d.get("Condition"))
        ):
            d["Condition"] = "Not Specified"
            changed = True
    return changed


def repair_sample(sample: dict) -> dict:
    """Apply both guards in place; returns per-guard change flags."""
    return {
        "treatment": apply_treatment_grounding(sample),
        "condition_cellline": apply_condition_cellline_guard(sample),
    }
