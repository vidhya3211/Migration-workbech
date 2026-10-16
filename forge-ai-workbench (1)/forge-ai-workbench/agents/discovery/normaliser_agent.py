"""Normaliser agent (T2): canonicalises every rule - name, domain, expression, conditions, confidence.

It doesn't care which parser a rule came from, only that every parser handed it the same shape. That is
what makes adding a file type cheap (§33). Code rules keep their canonical names; document claims and
workbook labels are matched to the code rule they describe by the name-match heuristic (ranked, with the
alternatives recorded - a heuristic never certifies).
"""
from __future__ import annotations

import re

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from pydantic import BaseModel

from agents.base import AgentOutput, BaseAgent
from blackboard.identity import get_identity
from reasoning.deterministic import canonical as canon
from reasoning.deterministic.expressions import ExpressionError, evaluate, identifiers
from reasoning.deterministic.naming import NAMING_VERSION, name_tokens
from reasoning.heuristics.confidence.aggregate import aggregate
from reasoning.heuristics.naming_match import score as name_match
from schemas.common import Tier, ReasoningMode
from schemas.rules import CandidateRule, NormalisedRule, RuleKind
from schemas.signals import SignalType

class NameChoice(BaseModel):
    canonical_name: str
    reason: str = ""


DEFAULT_MATCH_THRESHOLD = 0.7
LLM_MATCH_FLOOR = 0.4
DEFAULT_MATCH_MARGIN = 0.05


def source_kind(agent_name: str) -> str:
    """What kind of source a rule came from - declared by the extractor itself, not looked up by name here."""
    try:
        ident = get_identity(agent_name)
    except Exception:
        return "code"
    return ident.source_kind or "code"


def dialect(agent_name: str) -> str | None:
    try:
        return get_identity(agent_name).dialect
    except Exception:
        return None


def _domain(canonical: str, domains: dict[str, str]) -> str:
    tokens = name_tokens(canonical)
    for key, dom in domains.items():
        if key.upper() in tokens or any(t.startswith(key.upper()) for t in tokens):
            return dom
    return "general"


def _rename(expr: str, renames: dict[str, str]) -> str:
    if not renames:
        return expr
    return re.sub(r"\b[A-Z_][A-Z0-9_]*\b", lambda m: renames.get(m.group(), m.group()), expr)


@register_agent
class NormaliserAgent(BaseAgent):
    IDENTITY = agent_identity("normaliser_agent", Tier.T2, "discovery", 'Canonicalises every rule (name, domain, confidence)', {S.NORMALISED_RULES}, tools={'deterministic.expression'}, llm={'canonicalise_name'})

    def run(self, candidates: list[CandidateRule], **_: object) -> AgentOutput:
        settings = self.services.settings
        ncfg = settings.heuristics.get("normaliser", {})
        MATCH_THRESHOLD = float(ncfg.get("match_threshold", DEFAULT_MATCH_THRESHOLD))
        MATCH_MARGIN = float(ncfg.get("match_margin", DEFAULT_MATCH_MARGIN))
        code_names: dict[str, list[str]] = {}
        for r in candidates:
            if source_kind(r.agent) == "code" and r.kind in (RuleKind.CONSTANT, RuleKind.CALCULATION,
                                                                  RuleKind.ASSIGNMENT):
                code_names.setdefault(r.canonical_name, []).append(r.expression)
        renames: dict[str, str] = {}
        matched: dict[str, tuple[str, list[str]]] = {}
        for r in candidates:
            if source_kind(r.agent) not in ("document", "spreadsheet") or r.canonical_name in code_names:
                continue
            value = r.context.get("value") or (r.expression if r.kind == RuleKind.CONSTANT else None)
            scored = sorted((name_match(r.name, cand, phrase_value=value,
                                        candidate_values=[v for v in vals if _is_number(v)] or None,
                                        threshold=MATCH_THRESHOLD) for cand, vals in code_names.items()),
                            key=lambda h: -h.score)
            if scored and scored[0].passed:
                alts = [f"{h.candidate} ({h.score:.2f})" for h in scored[1:3] if h.score >= scored[0].score - MATCH_MARGIN]
                matched[r.rule_id] = (scored[0].candidate, [f"name matched to {scored[0].candidate} "
                                                            f"(score {scored[0].score:.2f})"] +
                                      ([f"close alternatives: {', '.join(alts)}"] if alts else []))
                if source_kind(r.agent) == "spreadsheet":
                    renames[r.canonical_name] = scored[0].candidate
            elif scored and scored[0].score >= LLM_MATCH_FLOOR:
                cands = [h.candidate for h in scored[:5]]
                shown = ", ".join(f"{h.candidate} ({h.score:.2f})" for h in scored[:5])
                context = str(r.context.get("sentence") or r.context.get("label") or r.name)
                draft, meta = self.narrate("canonicalise_name", {
                    "identifier": r.name, "context": context, "candidates": shown},
                    f"{r.name}\n{context}\n{shown}", NameChoice, text_of=lambda p: p.reason,
                    allowed=frozenset(cands) | {"NONE"}, value_of=lambda p: p.canonical_name)
                if draft is not None and draft.canonical_name != "NONE":
                    matched[r.rule_id] = (draft.canonical_name, [
                        f"name matched to {draft.canonical_name} with model assistance ({meta['model']}); "
                        f"heuristic score {scored[0].score:.2f} was below the threshold"])
                    if source_kind(r.agent) == "spreadsheet":
                        renames[r.canonical_name] = draft.canonical_name
        if matched:
            self.note_mode(ReasoningMode.HEURISTIC)
        out: list[NormalisedRule] = []
        failures = 0
        for r in candidates:
            kind_src = source_kind(r.agent)
            canonical, notes = matched.get(r.rule_id, (r.canonical_name, []))
            notes = list(notes)
            resolved, conditions = r.expression, list(r.conditions)
            ok = True
            try:
                resolved, conditions = self._canonicalise(r, kind_src, renames)
            except canon.CanonicalError as exc:
                ok = False
                failures += 1
                notes.append(f"expression not canonicalisable ({exc}); equivalence cannot be proved")
            constant = None
            variables: list[str] = []
            if ok and r.kind not in (RuleKind.CONSTRAINT, RuleKind.SCHEDULE, RuleKind.CONDITION_NAME, RuleKind.TABLE):
                try:
                    ids = identifiers(resolved)
                    for c in conditions:
                        ids |= identifiers(c)
                    variables = sorted(ids)
                    if not identifiers(resolved) and not conditions:
                        v = evaluate(resolved, {})
                        constant = str(v)
                except ExpressionError as exc:
                    ok = False
                    notes.append(f"canonical form does not evaluate: {exc}")
            conf = aggregate(r.rule_id, {"extraction": r.confidence},
                             penalties={"not_canonicalisable": 0.3} if not ok else {}).score
            out.append(NormalisedRule(
                rule_id=r.rule_id, canonical_name=canonical, domain=_domain(canonical, settings.domains), kind=r.kind,
                system=r.system, source_kind=kind_src, expression=r.expression,
                resolved_expression=resolved if ok else r.expression, constant_value=constant, variables=variables,
                condition_signature=conditions if ok else r.conditions, confidence=conf, notes=notes,
                evidence=r.evidence, agent=r.agent))
        self.note_mode(ReasoningMode.DETERMINISTIC)
        by_domain: dict[str, int] = {}
        for n in out:
            by_domain[n.domain] = by_domain.get(n.domain, 0) + 1
        self.signal(SignalType.NORMALISED_RULES, f"normalised {len(out)} rule(s); {len(matched)} document/workbook "
                    f"claim(s) matched to code rules; {failures} not canonicalisable",
                    item_refs=[n.rule_id for n in out],
                    data={"by_domain": by_domain, "matched": len(matched), "not_canonicalisable": failures,
                          "naming_version": NAMING_VERSION})
        return AgentOutput(items={"normalised": out})

    @staticmethod
    def _canonicalise(r: CandidateRule, kind_src: str, renames: dict[str, str]) -> tuple[str, list[str]]:
        ctx = r.context
        if r.kind == RuleKind.TABLE:          # entries compare as data, not as an expression
            return r.expression, []
        if r.kind in (RuleKind.SCHEDULE,) or kind_src == "jcl":
            return r.expression, list(r.conditions)
        d = dialect(r.agent)
        if d == "cobol":
            names = ctx.get("condition_names", {})
            conds = [canon.cobol(c, condition_names=names) for c in ctx.get("inherited_conditions", []) + r.conditions]
            if r.kind == RuleKind.CONDITION_NAME:
                return canon.cobol(r.expression), []
            expr = canon.cobol(r.expression, rounded_scale=ctx.get("rounded_scale"),
                               truncate_scale=ctx.get("truncate_scale"))
            return expr, conds
        if d == "php":
            owner = ctx.get("class")
            return canon.php(r.expression, owner_class=owner), [canon.php(c, owner_class=owner) for c in r.conditions]
        if d == "plsql":
            aliases = ctx.get("aliases", {})
            return canon.plsql(r.expression, aliases), [canon.plsql(c, aliases) for c in r.conditions]
        if d == "excel":
            expr = ctx.get("canonical_expression") if r.kind == RuleKind.CALCULATION else r.expression
            if expr is None:
                raise canon.CanonicalError(ctx.get("canonical_error", "no canonical form"))
            return _rename(expr, renames), []
        if d == "document":
            return canon.plsql(r.expression), [canon.plsql(c) for c in r.conditions]
        if d == "sql":
            if ctx.get("constraint") == "check":
                return canon.generic_sql(r.expression), []
            return r.expression, []
        return r.expression, list(r.conditions)


def _is_number(v: str) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False
