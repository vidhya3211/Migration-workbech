"""Entity-match agent (T2): finds the same customer across records - within an extract and across extracts
(a single customer view over every customer file in the estate) (UC10).

Blocking is done by PostgreSQL over *all* customer extracts at once, on several keys so one keying error does
not hide a match: date of birth, the strong identifier (e.g. national insurance number), and surname +
postcode (``heuristics.duplicate.blocking``, by role). Only the records of one block are ever loaded. Pair
scoring is explainable (``reasoning/heuristics/duplicate``): nickname-aware names, transposed dates of birth,
identifier agreement or conflict. ATS resolves blocks of more than two candidates (pair scores are not
transitive). Matched pairs whose records disagree on a configured field (``conflict_fields``, e.g. marketing
consent) carry the conflict, because merging them forces a choice.

Blocks larger than ``scale.entity_match_max_block`` are reported for the steward rather than paired (pairing is
quadratic). Names and identifiers never leave the controlled store: telemetry and signals carry record keys
only. A data steward confirms or rejects every match - the strong/ambiguous grade only orders the queue.
"""
from __future__ import annotations

from itertools import combinations

from agents.base import AgentOutput, BaseAgent
from agents.data_quality.extracts import Extract, load_extracts
from blackboard.identity import agent_identity, register_agent
from ingestion.pii import column_matches
from reasoning.ats.policies import EntityClusterPolicy
from reasoning.deterministic.transform import slash_order
from reasoning.heuristics.duplicate.scoring import score as dup_score
from schemas.common import ReasoningMode, Tier, stable_id, value_ref
from schemas.dq import EntityMatch
from schemas.mapping import DictionaryEntry
from schemas.signals import SignalLevel, SignalType
from schemas.signals import SignalType as S

DEFAULT_ROLES = {"name": ["name", "cust_name", "full_name"], "forename": ["forename", "first_name", "given_name"],
                 "surname": ["surname", "last_name", "family_name"],
                 "dob": ["dob", "date_of_birth", "birth_dt", "birth"],
                 "postcode": ["postcode", "post_code"], "national_id": ["ni_number", "national_insurance", "nino"]}
DEFAULT_BLOCKING = [["dob"], ["national_id"], ["surname", "postcode"]]


@register_agent
class EntityMatchAgent(BaseAgent):
    IDENTITY = agent_identity("entity_match_agent", Tier.T2, "data_quality", "Matches the same customer across records",
                              {S.ENTITY_MATCHES, S.HUMAN_REVIEW_REQUIRED}, tools={"evidence.read"}, ats=True,
                              proposes={"entity_match"})

    def run(self, dictionary: list[DictionaryEntry], **_: object) -> AgentOutput:
        st = self.services.settings
        cfg = st.heuristics.get("duplicate", {})
        threshold = float(cfg.get("threshold", 0.85))
        strong = float(cfg.get("strong_score", 0.9))
        nicknames = cfg.get("nicknames") or {}
        roles = {**DEFAULT_ROLES, **(cfg.get("roles") or {})}
        blocking = cfg.get("blocking") or DEFAULT_BLOCKING
        conflict_fields = [str(f).upper() for f in cfg.get("conflict_fields") or []]
        max_block = st.scale.entity_match_max_block
        order = slash_order()
        extracts: dict[str, tuple[Extract, dict[str, str]]] = {}
        for ex in load_extracts(self.services, self.run_id, "source", dictionary):
            cols = {role: next((c for c in ex.header if column_matches(c, names)), None) for role, names in roles.items()}
            cols = {r: c for r, c in cols.items() if c}
            if not (cols.get("name") or cols.get("surname")):
                continue
            extracts[ex.artefact.artefact_id] = (ex, cols)
        matches: dict[str, EntityMatch] = {}
        oversized = 0
        for block_roles in blocking:
            specs = [(aid, [ex.idx(cols[r]) for r in block_roles]) for aid, (ex, cols) in extracts.items()
                     if all(r in cols for r in block_roles)]
            if not specs:
                continue
            dates = {j for j, r in enumerate(block_roles) if r == "dob"}
            for _key, members, too_big in self.services.staging.blocks_multi(specs, max_block, dates, order):
                if too_big:
                    oversized += 1
                    continue
                recs = self._load(members, extracts)
                ids = sorted(recs)
                pairs = {}
                for a, b in combinations(ids, 2):     # re-scored per block (cheap) - no run-long cache
                    pairs[(a, b)] = dup_score(recs[a]["key"], recs[b]["key"], recs[a]["fields"], recs[b]["fields"],
                                              threshold, nicknames, strong, order)
                self.note_mode(ReasoningMode.HEURISTIC)
                accepted = {k: h for k, h in pairs.items() if h.passed}
                if len(ids) > 2 and accepted:
                    res = self.ats(EntityClusterPolicy(ids, {k: h.score for k, h in pairs.items()}, threshold))
                    clusters = res.best.state["clusters"] if res.best else []
                    keep = {(a, b) for c in clusters for a, b in combinations(sorted(c), 2)}
                    accepted = {k: h for k, h in accepted.items() if k in keep}
                for (a, b), h in accepted.items():
                    ra, rb = recs[a], recs[b]
                    mid = stable_id("EM", self.run_id, ra["key"], rb["key"])
                    if mid in matches or ra["key"] == rb["key"] and ra["aid"] == rb["aid"]:
                        continue
                    matches[mid] = EntityMatch(
                        match_id=mid, run_id=self.run_id, left_ref=ra["key"], right_ref=rb["key"], score=h,
                        evidence=[ra["ex"].evidence(ra["line"], ra["row"]), rb["ex"].evidence(rb["line"], rb["row"])],
                        grade="strong" if h.score >= strong else "ambiguous", blocked_on=list(block_roles),
                        conflicts=self._conflicts(ra, rb, conflict_fields))
        out = sorted(matches.values(), key=lambda m: (m.grade != "ambiguous", -m.score.score))
        strong_n = sum(1 for m in out if m.grade == "strong")
        cross = sum(1 for m in out if m.evidence[0].artefact_id != m.evidence[1].artefact_id)
        conflicts = sum(1 for m in out if m.conflicts)
        self.signal(SignalType.ENTITY_MATCHES,
                    f"{len(out)} probable same-customer pair(s) for steward review ({strong_n} strong, "
                    f"{len(out) - strong_n} ambiguous; {cross} across different files"
                    + (f"; {conflicts} with conflicting {', '.join(conflict_fields).lower()}" if conflicts else "") + ")"
                    + (f"; {oversized} block(s) above {max_block} records need a steward-defined blocking key"
                       if oversized else ""),
                    item_refs=[m.match_id for m in out][:500], level=SignalLevel.WARN if oversized else SignalLevel.INFO,
                    data={"matches": len(out), "strong": strong_n, "cross_file": cross, "with_conflicts": conflicts,
                          "oversized_blocks": oversized})
        return AgentOutput(items={"entity_matches": out})

    @staticmethod
    def _load(members: list[tuple[str, int]], extracts) -> dict[str, dict]:
        by_art: dict[str, list[int]] = {}
        for aid, line in members:
            by_art.setdefault(aid, []).append(line)
        out = {}
        for aid, lines in by_art.items():
            ex, cols = extracts[aid]
            rows = ex.rows_at(lines)
            for ln, row in rows.items():
                out[f"{aid}:{ln}"] = {"aid": aid, "line": ln, "row": row, "ex": ex, "key": ex.record_key(row),
                                      "fields": {r: ex.value(row, c) or "" for r, c in cols.items()}}
        return out

    @staticmethod
    def _conflicts(a: dict, b: dict, fields: list[str]) -> list[dict]:
        out = []
        for f in fields:
            ca = next((h for h in a["ex"].header if h.upper() == f), None)
            cb = next((h for h in b["ex"].header if h.upper() == f), None)
            if ca is None or cb is None:
                continue
            va, vb = (a["ex"].value(a["row"], ca) or "").strip(), (b["ex"].value(b["row"], cb) or "").strip()
            if va and vb and va.upper() != vb.upper():
                # value references only: the values stay in the controlled store (evidence shows them)
                out.append({"field": f, "left_ref": value_ref(va), "right_ref": value_ref(vb)})
        return out
