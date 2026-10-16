"""Canonical naming shared by extractors, the Normaliser and mapping heuristics.

Pipeline: strip scope/record prefixes (WS-, LS-, POL-) -> split camelCase / separators -> expand
abbreviations (FREQ -> FREQUENCY) -> apply the business synonym table (config). Deterministic and
versioned: a change here changes ``NAMING_VERSION`` and triggers evaluation.

The vocabulary is the *active project's* (``config.settings.active_settings``): each project may bring its own
synonyms, abbreviations and token synonyms. Derived tables are cached per vocabulary digest.
"""
from __future__ import annotations

import re

from config.settings import Settings, active_settings, settings_digest

NAMING_VERSION = "naming-1.2"
SCOPE_PREFIXES = {"WS", "LS", "WK", "W", "LK", "L", "THIS", "SELF", "STATIC", "V", "P", "C", "G", "POL", "CUST"}
VERB_PREFIXES = {"GET", "CALC", "CALCULATE", "COMPUTE", "DERIVE", "DETERMINE"}
GENERIC_METHODS = {"CALCULATE", "COMPUTE", "VALUE", "RUN", "EXECUTE", "APPLY", "GET", "HANDLE", "PROCESS", "INVOKE"}


def split_words(raw: str) -> list[str]:
    s = raw.strip()
    key = re.search(r"\[\s*['\"]([^'\"]+)['\"]\s*\]\s*$", s)   # $result['premium'] -> premium
    if key:
        s = key.group(1)
    s = re.split(r"::|->|\.", s)[-1]
    s = s.replace("$", "").replace("()", "")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return [p for p in re.split(r"[^A-Za-z0-9]+", s.upper()) if p]


_VOCAB: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
_TOKENS: dict[str, dict[str, str]] = {}


def _vocab() -> tuple[dict[str, str], dict[str, str]]:
    st = active_settings()
    key = settings_digest(st)
    hit = _VOCAB.get(key)
    if hit is None:
        hit = _build_vocab(st)
        if len(_VOCAB) > 64:
            _VOCAB.clear()
        _VOCAB[key] = hit
    return hit


def _build_vocab(st: Settings) -> tuple[dict[str, str], dict[str, str]]:
    abbrev = {k.upper(): v.upper() for k, v in st.abbreviations.items()}

    def expand(name: str) -> str:
        return "_".join(abbrev.get(w, w) for w in name.upper().split("_"))
    synonyms: dict[str, str] = {}
    for canon, alts in st.synonyms.items():
        c = expand(canon)
        synonyms[c] = c
        for alt in alts:
            synonyms[expand(alt)] = c
    return abbrev, synonyms


def canonical_name(raw: str, *, strip_verbs: bool = False, owner: str | None = None) -> str:
    parts = split_words(raw)
    while len(parts) > 1 and parts[0] in SCOPE_PREFIXES:
        parts.pop(0)
    if strip_verbs and len(parts) > 1 and parts[0] in VERB_PREFIXES:
        parts.pop(0)
    if owner and len(parts) == 1 and parts[0] in GENERIC_METHODS:   # SurrenderValue::calculate() -> SURRENDER_VALUE
        parts = split_words(owner)
    abbrev, synonyms = _vocab()
    name = "_".join(abbrev.get(p, p) for p in parts)
    return synonyms.get(name, name)


def _token_synonyms() -> dict[str, str]:
    st = active_settings()
    key = settings_digest(st)
    hit = _TOKENS.get(key)
    if hit is None:
        hit = {k.upper(): v.upper() for k, v in st.token_synonyms.items()}
        _TOKENS[key] = hit
    return hit


def name_tokens(name: str) -> set[str]:
    """Token set for similarity heuristics: abbreviations expanded, token synonyms folded (CHARGE ~ FEE)."""
    abbrev, _ = _vocab()
    ts = _token_synonyms()
    return {ts.get(t, t) for t in (abbrev.get(p, p) for p in split_words(name))} - {"THE", "OF", "A", "AN"}
