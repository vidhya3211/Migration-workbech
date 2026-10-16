"""Customer record linkage (UC10). Proposals only - a data steward confirms or rejects.

Signals, each explainable in the score components:

* a **strong identifier** (e.g. a national insurance number) equal on both records *with* the same surname is
  near-certain; the same identifier field holding *different* values is evidence against;
* **name** similarity after nickname canonicalisation (the project's nickname table) - forename and surname
  separately when the extract has them, the full name otherwise;
* **date of birth** equal, or day and month transposed (a classic keying error, partial credit);
* **postcode** equal.

The grade (``strong`` at or above ``strong_score``, otherwise ``ambiguous``) only orders the steward's queue.
"""
from __future__ import annotations

from reasoning.heuristics.base import jaro_winkler, make_result, norm_text
from schemas.reasoning import HeuristicResult

VERSION = "duplicate-2.0"
WEIGHTS = {"name": 0.4, "dob": 0.3, "postcode": 0.2, "national_id": 0.1}


def _names(rec: dict, nicknames: dict[str, str]) -> str:
    parts = [rec.get("forename") or "", rec.get("surname") or ""] if (rec.get("forename") or rec.get("surname")) \
        else [rec.get("name") or ""]
    tokens = norm_text(" ".join(parts)).split()
    return " ".join(nicknames.get(t, t) for t in tokens)


def _surname(rec: dict) -> str:
    if rec.get("surname"):
        return norm_text(rec["surname"])
    toks = norm_text(rec.get("name") or "").split()
    return toks[-1] if toks else ""


def date_key(v: str, order: str = "dmy") -> str:
    """YYYYMMDD for ISO, YYYYMMDD and 2-2-4 dates (day- or month-first by ``order``) - the blocking rule."""
    s = (v or "").strip()
    digits = s.replace("-", "").replace("/", "").replace(".", "")
    if len(s) == 10 and s[2] in "/.-" and s[5] in "/.-" and digits.isdigit():
        return digits[4:] + (digits[:2] + digits[2:4] if order == "mdy" else digits[2:4] + digits[:2])
    return digits if digits.isdigit() and len(digits) == 8 else s


def transposed(d1: str, d2: str) -> bool:
    a, b = norm_text(d1).replace(" ", ""), norm_text(d2).replace(" ", "")
    if len(a) != 8 or len(b) != 8 or not (a.isdigit() and b.isdigit()):
        return False
    # YYYYMMDD
    return a[:4] == b[:4] and a[4:6] == b[6:8] and a[6:8] == b[4:6] and a[4:6] != a[6:8]


def score(left_ref: str, right_ref: str, left: dict, right: dict, threshold: float,
          nicknames: dict[str, str] | None = None, strong_score: float = 0.9, date_order: str = "dmy") -> HeuristicResult:
    nick = {k.upper(): v.upper() for k, v in (nicknames or {}).items()}
    lid, rid = (left.get("national_id") or "").strip().upper(), (right.get("national_id") or "").strip().upper()
    weights = dict(WEIGHTS)
    if lid and rid and lid == rid and _surname(left) and _surname(left) == _surname(right):
        comps = {"national_id": 1.0, "name": 1.0, "dob": 1.0, "postcode": 1.0}
        res = make_result("duplicate.customer", VERSION, f"{left_ref}~{right_ref}", comps, weights, threshold,
                          ["strong identifier equal", "surname equal"], "strong identifier + surname: strong",
                          score=0.98)
        return res
    ld, rd = date_key(left.get("dob") or "", date_order), date_key(right.get("dob") or "", date_order)
    dob = 1.0 if ld and ld == rd else 0.8 if ld and rd and transposed(ld, rd) else 0.0
    comps = {
        "name": jaro_winkler(_names(left, nick), _names(right, nick)),
        "dob": dob,
        "postcode": 1.0 if left.get("postcode") and norm_text(left["postcode"]) == norm_text(right.get("postcode", "")) else 0.0,
    }
    evidence = [f"name similarity {comps['name']:.2f}",
                "DOB equal" if dob == 1.0 else "DOB day/month transposed" if dob else "DOB differs",
                "postcode equal" if comps["postcode"] else "postcode differs"]
    if lid and rid:
        comps["national_id"] = 1.0 if lid == rid else 0.0
        evidence.append("strong identifier equal" if lid == rid else "strong identifier CONFLICTS")
    else:
        weights.pop("national_id")
    res = make_result("duplicate.customer", VERSION, f"{left_ref}~{right_ref}", comps, weights, threshold, evidence,
                      "probabilistic record linkage (names never leave the controlled store)")
    if lid and rid and lid != rid:      # two different identifiers is evidence against, whatever the names say
        res = res.model_copy(update={"score": round(max(0.0, res.score - 0.15), 4),
                                     "passed": max(0.0, res.score - 0.15) >= threshold})
    grade = "strong" if res.score >= strong_score else "ambiguous"
    return res.model_copy(update={"reason": f"{res.reason}: {grade}"})
