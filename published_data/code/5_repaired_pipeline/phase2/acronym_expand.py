#!/usr/bin/env python3
"""Expand an acronym from the study text that defines it.

    Scientific prose defines its abbreviations in a regular shape: the
    expansion sits beside the acronym in parentheses. Extracting it is
    deterministic and cannot hallucinate, since the expansion is quoted
    from the study or not returned. The model is consulted only where the
    text is silent. Matching uses initial-letter alignment (Schwartz and
    Hearst, 2003), a rule about the shape of a definition rather than a
    dictionary, so no acronym is enumerated here.
    """

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-/]*")

# "long form (SF)"  and  "SF (long form)"
_PAREN = re.compile(r"\(([^()]{1,120})\)")

# The short form at the HEAD of a parenthetical, where the parenthetical goes on
# to say something else: "(MI; clinical symptoms of ...)", "(UA, n=31)".
_LEAD = re.compile(r"^([A-Za-z0-9][A-Za-z0-9'\-/]*)\s*(?:[;,:–—-]|$)")


def _leading_token(inner: str) -> str | None:
    """Candidate short form at the head of a parenthetical, else None.

    Schwartz and Hearst take the first token inside the parentheses as the
    candidate. Prose often writes 'myocardial infarction (MI; clinical
    symptoms ...)', so requiring the parentheses to hold the acronym alone
    loses the definition. The delimiter is required: a parenthetical that
    merely begins with a word is not a short form.
    """
    m = _LEAD.match((inner or "").strip())
    return m.group(1) if m else None


def _is_acronym_shaped(tok: str) -> bool:
    tok = tok.strip()
    if not (2 <= len(tok) <= 10):
        return False
    if not tok[0].isalpha():
        return False
    letters = [c for c in tok if c.isalpha()]
    if not letters:
        return False
    # mostly capitals, no spaces
    return " " not in tok and sum(c.isupper() for c in letters) >= max(2, len(letters) - 1)


def _align(short: str, long_words: list[str]) -> str | None:
    """Schwartz-Hearst: match the acronym's letters, right to left, against the
    letters of the candidate words; the acronym's first letter must begin a
    word. Returns the matched span of long_words, or None."""
    s = [c.lower() for c in short if c.isalnum()]
    if not s:
        return None
    text = " ".join(long_words)
    t = text.lower()
    si = len(s) - 1
    ti = len(t) - 1
    while si >= 0:
        ch = s[si]
        while ti >= 0 and t[ti] != ch:
            ti -= 1
        if ti < 0:
            return None
        if si == 0:
            # first acronym letter must start a word
            if ti != 0 and t[ti - 1] not in " -/":
                # keep searching further left for a word-initial occurrence
                ti -= 1
                while ti >= 0 and not (
                    t[ti] == ch and (ti == 0 or t[ti - 1] in " -/")
                ):
                    ti -= 1
                if ti < 0:
                    return None
            break
        si -= 1
        ti -= 1
    span = text[ti:].strip(" -/,;:")
    if not span:
        return None
    # a definition should not be absurdly longer than its acronym
    if len(span.split()) > min(len(s) + 5, len(s) * 2 + 2):
        return None
    return span


def find_definition(acronym: str, context: str) -> str | None:
    """Expansion of ``acronym`` as literally defined in ``context``, else None.

    Handles both orders: "chronic lymphocytic leukemia (CLL)" and
    "CLL (chronic lymphocytic leukemia)".
    """
    if not acronym or not context:
        return None
    acr = acronym.strip()
    if not _is_acronym_shaped(acr):
        return None

    for m in _PAREN.finditer(context):
        inner = m.group(1).strip()

        # form A: long form (ACRONYM), the parenthetical optionally going on
        # to gloss the term -- see _leading_token.
        head = _leading_token(inner)
        if head and head.lower().rstrip("s") == acr.lower().rstrip("s"):
            before = context[: m.start()]
            words = _WORD.findall(before)
            if not words:
                continue
            window = words[-min(len(acr) + 5, len(acr) * 2 + 2):]
            span = _align(acr, window)
            if span:
                return _clean(span)

        # form B: ACRONYM (long form)
        else:
            before = context[: m.start()].rstrip()
            tail = _WORD.findall(before)
            if tail and tail[-1].lower().rstrip("s") == acr.lower().rstrip("s"):
                if _plausible_expansion(acr, inner):
                    return _clean(inner)
    return None


def _plausible_expansion(acr: str, phrase: str) -> bool:
    """Accept "ACR (long form)" only when the phrase's initials support it, so a
    parenthetical aside ("AD (n=12)") is not mistaken for a definition."""
    words = [w for w in _WORD.findall(phrase) if w]
    if not words or len(words) > min(len(acr) + 5, len(acr) * 2 + 2):
        return False
    return _align(acr, words) is not None


def _clean(span: str) -> str:
    span = re.sub(r"\s+", " ", span).strip(" -/,;:.")
    # drop a leading article or dangling connective the window may have caught
    span = re.sub(r"^(?:the|a|an|of|in|with|from|and|for|to)\s+", "", span, flags=re.I)
    return span


# ---------------------------------------------------------------------------
# LLM fallback contract. Stated here so the prompt and the parser cannot drift
# apart again: the system prompt below names the exact tag the parser accepts.
# ---------------------------------------------------------------------------
SHORTFORM_SYSTEM = (
    "A GEO metadata label is a bare abbreviation/acronym (e.g. 'AD', 'CRC',\n"
    "'MS', 'HSC'). Using ONLY the provided study context, expand it to the\n"
    "full biomedical term it stands for IN THIS STUDY, in standard form\n"
    "(a disease, tissue/cell-type, or treatment name, as fits the field).\n"
    "If it is not an expandable abbreviation for a real biomedical entity\n"
    "(e.g. a stage/grade/sample code like 'T1' or 'G3', a group label, or\n"
    "the context does not tell you), answer KEEP. Never guess a meaning the\n"
    "context does not support.\n"
    "\n"
    "OUTPUT: on the VERY LAST line output exactly one of:\n"
    "  TERM: <full biomedical term>\n"
    "  TERM: KEEP\n"
)

_TERM = re.compile(r"TERM\s*:\s*(\S.*?)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_expansion(text: str, acronym: str) -> str | None:
    """Read the model's answer, tagged or bare.

    A tagged answer is preferred; an untagged single line is accepted rather
    than discarded, so a prompt and parser that drift apart cost precision
    on one value instead of recall on every value.
    """
    if not text:
        return None
    hits = _TERM.findall(text.strip())
    cand = hits[-1].strip() if hits else None
    if cand is None:
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        if len(lines) == 1 and len(lines[0]) <= 120:
            cand = lines[0]
    if not cand:
        return None
    cand = cand.strip().strip(".\"'")
    if not cand or cand.upper() == "KEEP":
        return None
    if cand.strip().lower() == (acronym or "").strip().lower():
        return None
    if _is_acronym_shaped(cand):          # an acronym is not an expansion
        return None
    return cand


def supports_acronym(acr: str, phrase: str) -> bool:
    """Could ``phrase`` be what ``acr`` abbreviates?

    Only the first letter is required. Demanding every letter measures the
    vocabulary rather than the answer: a disease name drops what the
    abbreviation keeps (HCV expands through 'virus'; the condition is filed
    as 'Hepatitis C'). The first letter is the one an abbreviation does not
    drop, and is what Schwartz and Hearst require to begin a word.

    Apply to a CONCEPT, over every name it carries, never to one string: a
    controlled vocabulary files terms under headings that need not spell the
    abbreviation, while their synonym lists do.
    """
    if not acr or not phrase:
        return False
    # A lowercase letter leading an otherwise capitalised abbreviation is a
    # qualifier, not part of the term: sPTD is spontaneous PTD, pABMR is
    # pABMR's own qualifier on ABMR. The core is what the concept must spell.
    core = _QUALIFIER_PREFIX.sub("", acr.strip())
    letters = [c.lower() for c in core if c.isalnum()]
    if not letters:
        return False
    # Hyphens join words inside one token, so split independently of _WORD:
    # 'Non-Small-Cell' supplies N, S and C, not N alone.
    initials = {w[0].lower() for w in re.findall(r"[A-Za-z0-9]+", phrase)}
    return letters[0] in initials


_QUALIFIER_PREFIX = re.compile(r"^[a-z](?=[A-Z]{2,})")


# ---------------------------------------------------------------------------
# Corpus-level acronym dictionary.
#
# Requiring the defining sentence to sit in the SAME study is what kept recall
# low even once extraction worked: 16,056 of NSCLC's 16,375 samples belong to
# studies that use the acronym without ever spelling it out. But a definition
# is evidence about the abbreviation, not about the study that happens to carry
# it, so definitions are pooled across the corpus and applied wherever the
# acronym appears.
#
# Pooling is only safe with an explicit conflict test, because some acronyms
# genuinely denote different things in different studies -- in this corpus AD is
# both Alzheimer disease and atopic dermatitis, MS is multiple sclerosis and
# mass spectrometry, MDS is myelodysplastic syndromes and multidimensional
# scaling. The test is run at the level of the resolved CONCEPT rather than the
# string, so that spelling variants ("leukemia"/"leukaemia", "cancer"/
# "carcinoma") count as agreement while genuinely different meanings count as
# conflict. A conflicted acronym is never expanded from the pool; it falls back
# to its own study's text, and stays out-of-vocabulary if that text is silent.
# ---------------------------------------------------------------------------


def harvest_definitions(context: str):
    """Every (acronym, definition) pair the passage defines, either order."""
    out = []
    if not context:
        return out
    for m in _PAREN.finditer(context):
        inner = m.group(1).strip()
        head = _leading_token(inner)
        if head and _is_acronym_shaped(head):
            words = _WORD.findall(context[: m.start()])
            if words:
                window = words[-min(len(head) + 5, len(head) * 2 + 2):]
                span = _align(head, window)
                if span:
                    out.append((head.upper(), _clean(span)))
        else:
            tail = _WORD.findall(context[: m.start()].rstrip())
            if tail and _is_acronym_shaped(tail[-1]) and _plausible_expansion(tail[-1], inner):
                out.append((tail[-1].upper(), _clean(inner)))
    return out


def build_corpus_dictionary(contexts, resolve_concept):
    """acronym -> expansion, for acronyms whose pooled definitions agree.

    ``contexts``        iterable of study texts.
    ``resolve_concept`` callable(str) -> concept id or None. Only definitions
                        that resolve are allowed to vote, so a typo abstains
                        instead of manufacturing a conflict.

    Returns (dictionary, conflicts) where conflicts maps a rejected acronym to
    the competing concepts, so an acronym is never silently dropped.
    """
    from collections import Counter, defaultdict

    votes = defaultdict(Counter)      # acronym -> concept -> n
    surface = defaultdict(dict)       # acronym -> concept -> best surface form
    for ctx in contexts:
        for acr, definition in harvest_definitions(ctx):
            cid = resolve_concept(definition)
            if not cid:
                continue
            votes[acr][cid] += 1
            surface[acr].setdefault(cid, definition)

    dictionary, conflicts = {}, {}
    for acr, counts in votes.items():
        if len(counts) == 1:
            cid = next(iter(counts))
            dictionary[acr] = surface[acr][cid]
        else:
            conflicts[acr] = sorted(counts, key=counts.get, reverse=True)
    return dictionary, conflicts


# ---------------------------------------------------------------------------
# Per-study disambiguation for acronyms the corpus defines more than one way.
#
# A pooled dictionary must refuse these -- AD is Alzheimer disease in 47 studies
# and atopic dermatitis in 20, and picking the majority would silently mislabel
# the minority. But refusing is not the same as having no evidence: the study
# that uses the acronym almost always names the disease somewhere in its own
# title, summary or design, just not in the "long form (ACR)" shape the
# definition parser looks for.
#
# So the competing expansions are scored against THIS study's text, and one is
# chosen only when the study's own words single it out. Ties and silence abstain
# to the model rather than guess, which keeps the precision the pooled path has.
# ---------------------------------------------------------------------------

_STOP_WORDS = {
    "disease", "diseases", "disorder", "disorders", "syndrome", "syndromes",
    "cancer", "cancers", "carcinoma", "carcinomas", "tumor", "tumour", "tumors",
    "neoplasm", "neoplasms", "chronic", "acute", "primary", "secondary", "cell",
    "cells", "type", "analysis", "human", "mouse", "patient", "patients",
    "study", "sample", "samples", "of", "the", "and", "in", "with",
}


def _distinctive(phrase: str):
    """Content words of an expansion, minus vocabulary shared by every disease
    name -- 'acute myeloid leukemia' contributes myeloid/leukemia, not acute."""
    return {w for w in re.findall(r"[a-z]{4,}", (phrase or "").lower())
            if w not in _STOP_WORDS}


def disambiguate(acronym: str, context: str, candidates: dict) -> str | None:
    """Choose among competing expansions using the study's own text.

    ``candidates`` maps concept id -> a surface form of that expansion.
    Returns the chosen surface form, or None to abstain.

    A candidate scores by how many of its distinctive words the study text
    contains. The winner must be strictly ahead of every rival: an equal score
    means the study does not distinguish them, and a zero score means it offers
    no evidence at all. Both abstain.
    """
    if not context or len(candidates) < 2:
        return None
    low = context.lower()
    scored = []
    for cid, phrase in candidates.items():
        words = _distinctive(phrase)
        if not words:
            continue
        hits = sum(1 for w in words if w in low)
        scored.append((hits, cid, phrase))
    if not scored:
        return None
    scored.sort(reverse=True)
    best = scored[0]
    if best[0] == 0:
        return None                       # study says nothing either way
    if len(scored) > 1 and scored[1][0] == best[0]:
        return None                       # study supports both equally
    return best[2]


def pooled_is_supported(expansion: str, context: str) -> bool:
    """Does THIS study's text back the pooled expansion?

    A pooled meaning applied to a study that never says the word is how an
    acronym acquires the wrong one. Conflict detection cannot cover this,
    since a meaning used but never defined casts no vote. Where the study is
    silent the acronym is left to the model, which reads the same context.
    """
    if not expansion or not context:
        return False
    low = context.lower()
    words = _distinctive(expansion)
    if not words:
        return False
    return any(w in low for w in words)
