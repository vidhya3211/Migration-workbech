"""Mapping heuristics: how strongly does a source attribute correspond to a target attribute?

Components: name (tokens after abbreviation expansion + synonyms + Jaro-Winkler), type compatibility,
length compatibility, domain (allowed-values) overlap, and data-profile similarity.
"""
from __future__ import annotations

from reasoning.deterministic.naming import canonical_name, name_tokens
from reasoning.deterministic.schema import COMPATIBLE, LOSSY
from reasoning.heuristics.base import containment, jaccard, jaro_winkler, make_result
from schemas.mapping import DictionaryEntry
from schemas.reasoning import HeuristicResult

VERSION = "mapping-heuristic-1.4"


def name_score(a: str, b: str) -> tuple[float, list[str]]:
    ca, cb = canonical_name(a), canonical_name(b)
    if ca == cb:
        return 1.0, [f"canonical names match ({ca})"]
    ta, tb = name_tokens(ca), name_tokens(cb)
    tok = max(jaccard(ta, tb), 0.9 * containment(ta, tb))
    jw = jaro_winkler(ca.replace("_", ""), cb.replace("_", ""))
    score = max(tok, 0.85 * jw)
    return round(score, 4), [f"{ca} vs {cb}: token {tok:.2f}, jaro-winkler {jw:.2f}"]


def entity_score(a: str, b: str) -> float:
    return name_score(a, b)[0]


def score_pair(src: DictionaryEntry, tgt: DictionaryEntry, weights: dict[str, float], threshold: float,
               profile_similarity: float | None = None) -> HeuristicResult:
    n, why = name_score(src.field, tgt.field)
    st, tt = src.data_type or "string", tgt.data_type or "string"
    type_c = 1.0 if st == tt else (0.6 if (st, tt) in COMPATIBLE and (st, tt) not in LOSSY else 0.35 if (st, tt) in COMPATIBLE else 0.0)
    if src.length and tgt.length:
        length_c = 1.0 if src.length <= tgt.length else max(0.0, 1 - (src.length - tgt.length) / src.length)
    else:
        length_c = 0.7
    if src.allowed_values and tgt.allowed_values:
        domain_c = jaccard(set(src.allowed_values), set(tgt.allowed_values))
    elif src.allowed_values or tgt.allowed_values:
        domain_c = 0.5
    else:
        domain_c = 0.7
    comps = {"name": n, "type": type_c, "length": round(length_c, 4), "domain": domain_c}
    if profile_similarity is not None:
        comps["profile"] = profile_similarity
    evidence = why + [f"type {st} -> {tt}", f"length {src.length} -> {tgt.length}"]
    res = make_result("mapping.attribute", VERSION, f"{src.entity}.{src.field}->{tgt.entity}.{tgt.field}", comps,
                      weights, threshold, evidence, "weighted attribute correspondence")
    # type/length compatibility alone is not correspondence: require name evidence or a strong data profile
    if res.passed and n < NAME_FLOOR and (profile_similarity or 0.0) < PROFILE_FLOOR:
        res = res.model_copy(update={"passed": False, "reason": f"no name evidence (name {n:.2f} < {NAME_FLOOR}) "
                                                               "and no strong profile match"})
    return res


NAME_FLOOR = 0.5
PROFILE_FLOOR = 0.8
