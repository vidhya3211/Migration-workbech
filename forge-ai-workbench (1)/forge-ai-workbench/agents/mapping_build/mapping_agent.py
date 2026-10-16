"""Mapping agent (T2): proposes attribute-level source -> target mappings (UC6).

    deterministic - entity pairing inputs, type/length/domain facts, transformation inference
    heuristic     - weighted attribute correspondence (name/type/length/domain/profile)
    ATS           - when candidates are close or contend for the same target attribute
    LLM           - only if still ambiguous and a model is permitted/available
A data steward approves every mapping (Stage 6).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from pydantic import BaseModel, Field

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from agents.data_quality.extracts import Extract, load_extracts
from context.headroom.policy import ContentClass
from reasoning.ats.policies import MappingAssignmentPolicy
from reasoning.heuristics.base import jaccard
from reasoning.heuristics.mapping.heuristics import entity_score, score_pair
from reasoning.llm.gateway import Evidence as LLMEvidence
from reasoning.router.router import ReasoningTask
from schemas.common import Tier, ReasoningMode, stable_id
from schemas.mapping import DictionaryEntry, MappingProposal, TransformationSpec
from schemas.reasoning import HeuristicResult
from schemas.signals import SignalType


class Ranking(BaseModel):
    ranking: list[dict] = Field(default_factory=list)


class TransformDraft(BaseModel):
    kind: str
    params: dict = Field(default_factory=dict)
    description: str


TRANSFORM_KINDS = frozenset({"direct", "cast", "code_map", "date_format"})


def _shape(v: str) -> str:
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", v.strip()))[:20]


def profile_similarity(src_vals: list[str], tgt_vals: list[str]) -> float | None:
    if not src_vals or not tgt_vals:
        return None
    s = {k for k, _ in Counter(_shape(v) for v in src_vals if v.strip()).most_common(5)}
    t = {k for k, _ in Counter(_shape(v) for v in tgt_vals if v.strip()).most_common(5)}
    return jaccard(s, t)


def infer_transformation(src: DictionaryEntry, tgt: DictionaryEntry, observed: list[str]) -> TransformationSpec:
    st, tt = src.data_type or "string", tgt.data_type or "string"
    values = sorted({v.strip() for v in observed if v.strip()})
    if tt == "boolean" and st == "string":
        truthy = {"Y": "true", "N": "false", "T": "true", "F": "false", "1": "true", "0": "false"}
        return TransformationSpec(kind="code_map", params={"map": {k: v for k, v in truthy.items() if k in values or k in "YN"},
                                                            "strict": True},
                                  description="flag Y/N -> boolean true/false")
    if tt == "date" and st in ("string", "date"):
        eight = bool(values) and all(re.fullmatch(r"\d{8}", v) for v in values) or (src.length == 8)
        return TransformationSpec(kind="date_format", params={"from": "%Y%m%d" if eight else "iso",
                                                               "null_sentinels": ["00000000", "99999999", ""]},
                                  description="legacy YYYYMMDD date -> ISO date; sentinels become NULL" if eight
                                  else "date -> ISO date")
    if st in ("integer", "decimal") and tt in ("integer", "decimal"):
        if tt == "decimal" and tgt.scale is not None and tgt.scale != (src.scale or 0):
            return TransformationSpec(kind="cast", params={"to": "decimal", "scale": tgt.scale},
                                      description=f"decimal, rescaled to {tgt.scale} places")
        return TransformationSpec(kind="direct", params={}, description="numeric value copied exactly")
    if tt in ("integer", "decimal") and st == "string":
        return TransformationSpec(kind="cast", params={"to": tt}, description=f"string -> {tt} (checked)")
    return TransformationSpec(kind="direct", params={}, description="value copied exactly")


@register_agent
class MappingAgent(BaseAgent):
    IDENTITY = agent_identity("mapping_agent", Tier.T2, "mapping_build", 'Proposes attribute-level source->target mappings', {S.MAPPING_PROPOSALS}, llm={'semantic_mapping_tiebreak', 'transformation_plan'}, ats=True, proposes={'mapping'})

    def run(self, dictionary: list[DictionaryEntry], **_: object) -> AgentOutput:
        cfg = self.services.settings.heuristics.get("mapping", {})
        weights, threshold = cfg.get("weights", {}), float(cfg.get("accept_threshold", 0.55))
        margin = float(cfg.get("ambiguity_margin", 0.08))
        src = [d for d in dictionary if d.system.system_role.value == "source"]
        tgt = [d for d in dictionary if d.system.system_role.value == "target"]
        src_x = {e.entity: e for e in load_extracts(self.services, self.run_id, "source", dictionary) if e.entity}
        tgt_x = {e.entity: e for e in load_extracts(self.services, self.run_id, "target", dictionary) if e.entity}
        by_se, by_te = defaultdict(list), defaultdict(list)
        for d in src:
            by_se[d.entity].append(d)
        for d in tgt:
            by_te[d.entity].append(d)
        proposals: list[MappingProposal] = []
        unmapped: list[str] = []
        pairs = self._pair_entities(by_se, by_te, weights, threshold)
        self.note_mode(ReasoningMode.DETERMINISTIC)
        self.note_mode(ReasoningMode.HEURISTIC)
        for se, te in pairs:
            cands: dict[str, list[HeuristicResult]] = {}
            for s in by_se[se]:
                scored = []
                for t in by_te[te]:
                    prof = profile_similarity(self._col(src_x.get(se), s.field), self._col(tgt_x.get(te), t.field))
                    scored.append(score_pair(s, t, weights, threshold, prof))
                cands[s.field] = sorted([h for h in scored if h.passed], key=lambda h: -h.score)
            best_target = {s: c[0].candidate.split("->")[1] for s, c in cands.items() if c}
            contended = {t for t, n in Counter(best_target.values()).items() if n > 1}
            close = [s for s, c in cands.items() if len(c) > 1 and c[0].score - c[1].score < margin]
            assignment = dict(best_target)
            mode = ReasoningMode.HEURISTIC
            notes_for: dict[str, list[str]] = defaultdict(list)
            plan = self.plan(ReasoningTask(task=f"map {se}->{te}", deterministic_possible=True,
                                           candidates=sum(len(c) for c in cands.values()),
                                           close_alternatives=len(close) + len(contended)))
            if ReasoningMode.ATS in plan.route:
                ambiguous = [s for s in cands if cands[s] and (s in close or best_target.get(s) in contended)]
                taken = {t for s, t in best_target.items() if s not in ambiguous}
                res = self.ats(MappingAssignmentPolicy(
                    ambiguous, {s: [h.model_copy(update={"candidate": f"{s}->{h.candidate.split('->')[1]}"})
                                    for h in cands[s]] for s in ambiguous}, taken=taken))
                if res.best is not None:
                    for s, t in res.best.state["assigned"].items():   # t is "ENTITY.field" or None (unmapped)
                        assignment[s] = t
                        notes_for[s].append(f"resolved by ATS {res.ats_run_id} (confidence {res.best.confidence:.2f})")
                    mode = ReasoningMode.ATS
                else:
                    for s in ambiguous:
                        assignment.pop(s, None)
                        notes_for[s].append(f"ambiguous; ATS {res.ats_run_id} found no confident assignment")
                    self._llm_tiebreak(se, te, list(ambiguous), cands, notes_for)
            for s_entry in by_se[se]:
                t_name = assignment.get(s_entry.field)
                if not t_name:
                    unmapped.append(f"{se}.{s_entry.field}")
                    continue
                t_field = t_name.split(".", 1)[1] if "." in t_name else t_name
                t_entry = next((x for x in by_te[te] if x.field == t_field), None)
                if t_entry is None:
                    unmapped.append(f"{se}.{s_entry.field}")
                    continue
                hs = cands[s_entry.field]
                chosen = next((h for h in hs if h.candidate.endswith(f".{t_field}")), hs[0] if hs else None)
                observed = self._col(src_x.get(se), s_entry.field)
                transformation = infer_transformation(s_entry, t_entry, observed)
                notes = []
                types = {s_entry.data_type or "string", t_entry.data_type or "string"}
                if transformation.kind == "direct" and len(types) > 1 and types != {"integer", "decimal"}:
                    transformation, notes = self._plan_transformation(s_entry, t_entry, observed, transformation)
                proposals.append(MappingProposal(
                    mapping_id=stable_id("MAP", self.run_id, se, s_entry.field, te, t_field), run_id=self.run_id,
                    source=s_entry, target=t_entry, transformation=transformation,
                    score=chosen.score if chosen else 0.0, heuristics=hs[:3],
                    alternatives=[h.candidate for h in hs[1:3]], reasoning_mode=mode if notes_for.get(s_entry.field) else ReasoningMode.HEURISTIC,
                    reasoning_notes=notes_for.get(s_entry.field, []) + notes + ([chosen.reason] if chosen else [])))
        self.signal(SignalType.MAPPING_PROPOSALS, f"{len(proposals)} mapping proposal(s); {len(unmapped)} source "
                    "attribute(s) without a target", item_refs=[p.mapping_id for p in proposals],
                    data={"entity_pairs": [f"{a}->{b}" for a, b in pairs], "unmapped": unmapped[:50]})
        return AgentOutput(items={"mappings": proposals, "unmapped": unmapped})

    def _pair_entities(self, by_se, by_te, weights, threshold) -> list[tuple[str, str]]:
        pairs = []
        for se, s_fields in by_se.items():
            best, best_score = None, 0.0
            for te, t_fields in by_te.items():
                coverage = sum(1 for s in s_fields if any(score_pair(s, t, weights, threshold).passed for t in t_fields))
                sc = 0.5 * entity_score(se, te) + 0.5 * coverage / max(1, len(s_fields))
                if sc > best_score:
                    best, best_score = te, sc
            if best and best_score >= 0.4:
                pairs.append((se, best))
        return pairs

    def _col(self, ex: Extract | None, column: str) -> list[str]:
        """A bounded sample of observed values (profile similarity and transformation inference)."""
        if ex is None or column not in ex.header:
            return []
        return ex.sample(column, self.services.settings.scale.mapping_profile_sample)

    def _plan_transformation(self, s: DictionaryEntry, t: DictionaryEntry, observed: list[str],
                             inferred: TransformationSpec) -> tuple[TransformationSpec, list[str]]:
        """Types disagree and the deterministic inference fell back to a direct copy: ask for a plan (where a model
        is permitted). The validation agent then dry-runs it over every distinct source value."""
        sample = sorted({v.strip() for v in observed if v and v.strip()})[:40]
        facts = f"source {s.entity}.{s.field} {s.data_type} values {sample}; target {t.entity}.{t.field} {t.data_type}"
        draft, meta = self.narrate("transformation_plan", {
            "source": f"{s.entity}.{s.field} ({s.data_type}, length {s.length}, scale {s.scale})",
            "target": f"{t.entity}.{t.field} ({t.data_type}, length {t.length}, scale {t.scale})",
            "sample": ", ".join(sample), "inferred": inferred.description}, facts, TransformDraft,
            text_of=lambda p: p.description, allowed=TRANSFORM_KINDS, value_of=lambda p: p.kind)
        if draft is None:
            return inferred, []
        return (TransformationSpec(kind=draft.kind, params=draft.params,
                                   description=f"{draft.description} (model-proposed plan; validated by dry run)"),
                [f"transformation plan proposed by {meta['model']} ({meta['prompt_version']}); steward approves"])

    def _llm_tiebreak(self, se: str, te: str, fields: list[str], cands, notes_for) -> None:
        plan = self.plan(ReasoningTask(task=f"semantic tiebreak {se}->{te}", deterministic_possible=False,
                                       needs_semantics=True))
        if plan.requires_human_interpretation:
            for s in fields:
                notes_for[s].append("left for the data steward (no LLM permitted/available)")
            return
        for s in fields:
            cand_text = "; ".join(f"{h.candidate} ({h.score:.2f})" for h in cands[s][:3])
            res = self.llm("semantic_mapping_tiebreak", {"source": f"{se}.{s}", "candidates": cand_text},
                           [LLMEvidence("candidates", cand_text, ContentClass.STRUCTURED_BULK)], Ranking)
            if res.parsed and res.parsed.ranking:
                notes_for[s].append(f"LLM ranking suggestion: {res.parsed.ranking[0].get('candidate')} "
                                    "(advisory; steward decides)")
