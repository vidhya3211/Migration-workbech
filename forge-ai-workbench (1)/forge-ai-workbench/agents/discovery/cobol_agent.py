"""COBOL agent (T1): parses COBOL and proposes candidate rules with line-level evidence.

T1: gathers evidence; a named human writes the answer. Beyond the parse, each rule carries what the
Normaliser needs to *prove* equivalence later: 88-level condition expansions, the PIC scale that
ROUNDED / truncation apply at, and conditions inherited from the PERFORM site that runs the paragraph.
Statements the parser cannot model go to the LLM (if permitted and available) - and whatever the LLM
proposes is re-anchored to the real source lines and must pass the deterministic guard like everything else.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from tools.parsers.tables import cobol_tables
from agents.discovery.common import table_rule, lines_excerpt, make_rule
from context.headroom.policy import ContentClass
from reasoning.deterministic.naming import canonical_name
from reasoning.llm.gateway import Evidence as LLMEvidence
from reasoning.router.router import ReasoningTask
from schemas.artefact import Artefact
from schemas.common import Tier, ReasoningMode
from schemas.provenance import CodeLocator
from schemas.rules import CandidateRule, RuleKind
from schemas.signals import SignalLevel, SignalType
from tools.parsers.cobol import (
    INIT_FIGURATIVE, DataItem, ProcedureParser, inputs_of, is_literal, normalise, parse_data_items, split_sentences,
    tokenize,
)

_DIVISION = re.compile(r"\s*(IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b", re.I)


def pic_scale(pic: str | None) -> int | None:
    if not pic:
        return None
    p = re.sub(r"(\w)\((\d+)\)", lambda m: m.group(1) * int(m.group(2)), pic.upper())
    if "V" not in p:
        return 0 if re.search(r"9", p) else None
    return p.split("V", 1)[1].count("9")


def condition_expansion(item: DataItem) -> str | None:
    if item.level != 88 or not item.parent or not item.values:
        return None
    parts = []
    for v in item.values:
        m = re.match(r"^(.+?)\s+THRU\s+(.+)$", v, re.I)
        parts.append(f"{m.group(1)} <= {item.parent} <= {m.group(2)}" if m else None)
    if all(p is None for p in parts):
        return f"{item.parent} = {item.values[0]}" if len(item.values) == 1 else \
            f"{item.parent} IN ({', '.join(item.values)})"
    ors = [p if p else f"{item.parent} = {v}" for p, v in zip(parts, item.values)]
    return " OR ".join(f"({o})" for o in ors)


class LLMRule(BaseModel):
    name: str
    kind: str
    expression: str
    conditions: list[str] = Field(default_factory=list)
    line_start: int
    line_end: int
    anchor: str
    rationale: str = ""


class LLMRules(BaseModel):
    rules: list[LLMRule] = Field(default_factory=list)


@register_agent
class CobolAgent(BaseAgent):
    IDENTITY = agent_identity("cobol_agent", Tier.T1, "discovery", 'Parses COBOL, proposes rules with evidence', {S.CANDIDATE_RULES}, tools={'evidence.read'}, llm={'interpret_unparsed_statement'}, proposes={'rule'}, extractor=True, source_kind="code", dialect="cobol", handles={'cobol_program', 'cobol_copybook'})

    def run(self, artefact: Artefact, **_: object) -> AgentOutput:
        text = self.services.reader.text(artefact.blob_uri) or ""
        lines = text.split("\n")
        copybooks = self._copybook_index(artefact)
        if artefact.artefact_type == "cobol_copybook":
            rules, summary = self._copybook(artefact, text, lines)
        else:
            rules, summary = self._program(artefact, text, lines, copybooks)
        self.signal(SignalType.CANDIDATE_RULES, f"{artefact.path}: {len(rules)} candidate rule(s)",
                    item_refs=[r.rule_id for r in rules], data=summary, artefact_id=artefact.artefact_id)
        return AgentOutput(items={"candidates": rules}, summary=summary)

    # ------------------------------------------------------------------ copybooks
    def _copybook_index(self, artefact: Artefact) -> dict[str, list[DataItem]]:
        index: dict[str, list[DataItem]] = {}
        for art in self.services.artefacts(self.run_id).values():
            if art.artefact_type == "cobol_copybook" and art.system == artefact.system:
                src, _ = normalise(self.services.reader.text(art.blob_uri) or "")
                items, _ = parse_data_items(split_sentences(src), source=art.path)
                index[PurePosixPath(art.path).stem.upper()] = items
        return index

    def _copybook(self, art: Artefact, text: str, lines: list[str]) -> tuple[list[CandidateRule], dict]:
        src, fixed = normalise(text)
        items, copies = parse_data_items(split_sentences(src), source=art.path)
        rules = self._data_rules(art, lines, items, modified=None)
        tables = cobol_tables(items)       # evidence is the copybook's own lines
        rules += [table_rule(agent=self.name, tier=self.identity.tier, art=art, table=tb, lines=lines) for tb in tables]
        self.note_mode(ReasoningMode.DETERMINISTIC)
        return rules, {"mode": "copybook", "tables": len(tables), "format": "fixed" if fixed else "free", "data_items": len(items),
                       "nested_copies": copies, "rules": len(rules)}

    # ------------------------------------------------------------------ programs
    def _program(self, art: Artefact, text: str, lines: list[str], copybooks: dict[str, list[DataItem]]):
        src, fixed = normalise(text)
        divisions: dict[str, int] = {}
        for idx, sl in enumerate(src):
            m = _DIVISION.match(sl.code)
            if m:
                divisions.setdefault(m.group(1).upper(), idx)
        proc_idx, data_idx = divisions.get("PROCEDURE"), divisions.get("DATA")
        own_items: list[DataItem] = []
        data_copies: list[str] = []
        if data_idx is not None:
            own_items, data_copies = parse_data_items(
                split_sentences(src[data_idx + 1: proc_idx if proc_idx is not None else len(src)]), source=art.path)
        resolved, unresolved = [], []
        all_items = list(own_items)
        for member in data_copies:
            if member in copybooks:
                resolved.append(member)
                all_items.extend(copybooks[member])
            else:
                unresolved.append(member)
                self.signal(SignalType.CANDIDATE_RULES, f"{art.path}: COPY {member} not in this upload; identifiers "
                            "from it cannot be verified", level=SignalLevel.WARN, artefact_id=art.artefact_id)
        dd: dict[str, DataItem] = {}
        for item in all_items:
            if item.name != "FILLER":
                dd.setdefault(item.name.upper(), item)
        cond_names = {n: e for n, it in dd.items() if (e := condition_expansion(it))}

        parser = ProcedureParser([], dd)
        if proc_idx is not None:
            toks = tokenize(src[proc_idx:])
            first_dot = next((k for k, t in enumerate(toks) if t.kind == "dot"), len(toks) - 1)
            parser = ProcedureParser(toks[first_dot + 1:], dd).run()
        call_sites: dict[str, list] = defaultdict(list)
        for call in parser.calls:
            call_sites[call.target.upper()].append(call)

        def inherited(par: str | None, depth: int = 0) -> list[str]:
            if not par or depth > 8:
                return []
            sites = call_sites.get(par.upper(), [])
            if len(sites) != 1:
                return []
            site = sites[0]
            return inherited(site.caller, depth + 1) + list(site.conditions)

        constants = {n: it for n, it in dd.items() if it.level != 88 and it.value is not None
                     and it.value.upper() not in INIT_FIGURATIVE and n not in parser.modified}
        rules = self._data_rules(art, lines, own_items, modified=parser.modified)
        rules += [table_rule(agent=self.name, tier=self.identity.tier, art=art, table=tb, lines=lines)
                  for tb in cobol_tables(own_items)]       # copybook tables are lifted when the copybook is read
        for f in parser.findings:
            inputs = inputs_of(f.expr_toks + f.cond_toks, dd)
            if f.kind == "calculation":
                conf, reasons = 0.9, [f"deterministic parse of {f.verb}"]
            elif f.verb == "SET" or is_literal(f.expr_toks):
                conf, reasons = 0.85, ["literal assignment"]
            elif f.expr_toks and f.expr_toks[0].upper == "FUNCTION":
                conf, reasons = 0.8, ["intrinsic FUNCTION result"]
            else:
                conf, reasons = 0.65, ["conditional data move (no literal)"]
            if dd:
                missing = [x for x in [f.anchor] + inputs if x.upper() not in dd]
                if missing:
                    conf -= 0.2
                    reasons.append("not declared in program or supplied copybooks: " + ", ".join(dict.fromkeys(missing)))
            notes = list(f.notes)
            for name in inputs:
                item = constants.get(name.upper())
                if item is not None:
                    origin = "" if item.source == art.path else f" in {item.source}"
                    notes.append(f"{name} = {item.value} (VALUE clause line {item.start_line}{origin}; never modified)")
            if f.loops:
                notes.append("executes inside " + "; ".join(f.loops))
            target_item = dd.get(f.target.split("(")[0].split()[0].upper())
            scale = pic_scale(target_item.pic) if target_item else None
            ctx = {"verb": f.verb, "paragraph": f.paragraph, "condition_lines": f.condition_lines,
                   "performed_by": [f"{c.caller or '(entry)'} line {c.line}" for c in call_sites.get((f.paragraph or "").upper(), [])],
                   "inherited_conditions": inherited(f.paragraph),
                   "condition_names": {n: e for n, e in cond_names.items()
                                       if any(t.upper == n for t in f.cond_toks) or n in (c.upper() for c in _words(inherited(f.paragraph)))},
                   "pic": target_item.pic if target_item else None}
            if f.kind == "calculation" and scale is not None:
                ctx["rounded_scale" if "ROUNDED" in f.notes else "truncate_scale"] = scale
            rules.append(make_rule(
                agent=self.name, tier=self.identity.tier, art=art, kind=f.kind, name=f.target,
                canonical=canonical_name(f.target), expression=f.expression,
                locator=CodeLocator(file=art.path, line_start=f.start_line, line_end=f.end_line, symbol=f.paragraph),
                excerpt=lines_excerpt(lines, f.start_line, f.end_line), anchor=f.anchor, confidence=conf,
                reasons=reasons, conditions=f.conditions, inputs=inputs, notes=notes, context=ctx))
        rules.extend(self._unparsed(art, lines, parser.skipped))
        self.note_mode(ReasoningMode.DETERMINISTIC)
        return rules, {
            "mode": "program", "format": "fixed" if fixed else "free", "divisions": sorted(divisions),
            "data_items": len(own_items), "copybooks_resolved": resolved, "copybooks_unresolved": unresolved,
            "paragraphs": len(parser.paragraphs), "perform_edges": len(parser.calls), "sql_blocks": parser.sql_blocks,
            "sql_tables": sorted(parser.sql_tables), "unconditional_data_moves": parser.data_moves,
            "skipped_statements": len(parser.skipped), "rules": len(rules),
        }

    def _data_rules(self, art: Artefact, lines: list[str], items: list[DataItem], modified: set[str] | None):
        rules: list[CandidateRule] = []
        for it in items:
            if it.name == "FILLER":
                continue
            scope = f"{it.section} SECTION" if it.section else None
            loc = CodeLocator(file=art.path, line_start=it.start_line, line_end=it.end_line, symbol=scope)
            excerpt = lines_excerpt(lines, it.start_line, it.end_line)
            if it.level == 88 and it.values and it.parent:
                expr = condition_expansion(it) or ""
                rules.append(make_rule(
                    agent=self.name, tier=self.identity.tier, art=art, kind=RuleKind.CONDITION_NAME, name=it.name,
                    canonical=canonical_name(it.name), expression=expr, locator=loc, excerpt=excerpt, anchor=it.name,
                    confidence=0.95, reasons=["declarative 88-level condition name"], inputs=[it.parent],
                    context={"level": 88, "parent": it.parent, "values": it.values}))
            elif (it.value is not None and it.level != 88 and it.section not in ("FILE", "LINKAGE")
                  and it.value.upper() not in INIT_FIGURATIVE):
                if modified is None:
                    conf, why = 0.6, "initial VALUE in a copybook; runtime changes are not visible from here"
                elif it.name.upper() in modified:
                    conf, why = 0.5, "initial VALUE only; the item is modified in the PROCEDURE DIVISION"
                else:
                    conf, why = 0.9, "VALUE clause; never modified in the PROCEDURE DIVISION"
                notes = [f"PIC {it.pic}"] if it.pic else []
                if it.usage:
                    notes.append(f"USAGE {it.usage}")
                rules.append(make_rule(
                    agent=self.name, tier=self.identity.tier, art=art, kind=RuleKind.CONSTANT, name=it.name,
                    canonical=canonical_name(it.name), expression=it.value, locator=loc, excerpt=excerpt,
                    anchor=it.name, confidence=conf, reasons=[why], notes=notes,
                    context={"level": it.level, "parent": it.parent, "pic": it.pic,
                             "modified": bool(modified and it.name.upper() in modified)}))
        return rules

    def _unparsed(self, art: Artefact, lines: list[str], skipped: list[tuple[int, str]]) -> list[CandidateRule]:
        if not skipped:
            return []
        plan = self.plan(ReasoningTask(task="interpret unparsed COBOL", deterministic_possible=False,
                                       needs_semantics=True))
        if plan.requires_human_interpretation:
            self.signal(SignalType.CANDIDATE_RULES,
                        f"{art.path}: {len(skipped)} statement(s) not modelled deterministically; routed to a human "
                        f"(lines {', '.join(str(ln) for ln, _ in skipped[:10])})", level=SignalLevel.WARN,
                        data={"unparsed": [{"line": ln, "why": why} for ln, why in skipped]},
                        artefact_id=art.artefact_id)
            return []
        out: list[CandidateRule] = []
        for line, why in skipped[:5]:
            lo, hi = max(1, line - 2), min(len(lines), line + 4)
            ev = "\n".join(f"{n:>5} | {lines[n - 1]}" for n in range(lo, hi + 1))
            res = self.llm("interpret_unparsed_statement", {"language": "COBOL", "file": art.path},
                           [LLMEvidence("source", ev, ContentClass.SOURCE_CODE_EVIDENCE)], LLMRules)
            if res.parsed is None:
                continue
            for r in res.parsed.rules[:1]:
                if not (lo <= r.line_start <= r.line_end <= hi) or r.kind not in ("calculation", "assignment", "constant"):
                    continue   # the model cited lines outside what it was shown - refused
                out.append(make_rule(
                    agent=self.name, tier=self.identity.tier, art=art, kind=r.kind, name=r.name,
                    canonical=canonical_name(r.name), expression=r.expression,
                    locator=CodeLocator(file=art.path, line_start=r.line_start, line_end=r.line_end),
                    excerpt=lines_excerpt(lines, r.line_start, r.line_end), anchor=r.anchor, confidence=0.5,
                    reasons=[f"LLM interpretation of a statement the parser could not model ({why})"],
                    conditions=r.conditions, notes=[r.rationale[:300]], context={"verb": "LLM"},
                    mode=ReasoningMode.LLM, model=res.record.model, prompt_version=res.record.prompt_version))
        return out


def _words(conds: list[str]) -> list[str]:
    return [w for c in conds for w in re.findall(r"[A-Za-z0-9-]+", c)]
