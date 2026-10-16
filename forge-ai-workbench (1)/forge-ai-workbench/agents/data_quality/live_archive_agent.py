"""Live/archive agent (T1): scores each policy's probability of still being active (UC9).

Deterministic features (status meaning, premium/payment recency, date validity) combined by an explainable
weighting. Recommendation only. Status meanings, weights, the recency horizon and the cut-offs are project
configuration (``heuristics.live_archive``) - a different administration system brings its own codes.

Overrides (``overrides``: a condition and a floor) lift records that must not be archived whatever their
activity says - a claim in progress at cutover is live. The summary carries the threshold curve (how many
records would be treated as live at each cut-off) so the programme can choose the cut-off with its costs in view.

Scores stream to PostgreSQL (``live_archive_score``) as they are computed; graph state carries only the
summary, so ten million policies cost the same memory as ten.
"""
from __future__ import annotations

from datetime import date

from agents.base import AgentOutput, BaseAgent
from agents.data_quality.extracts import load_extracts
from blackboard.identity import agent_identity, register_agent
from reasoning.deterministic.naming import canonical_name
from reasoning.deterministic.predicates import describe, fields_of, matches, row_getter
from reasoning.deterministic.transform import TransformError, parse_date
from schemas.common import ReasoningMode, Tier
from schemas.mapping import DictionaryEntry
from schemas.signals import SignalType
from schemas.signals import SignalType as S

DEFAULTS = {"status_column": "STATUS", "status_live": {"A": 1.0, "L": 0.05, "P": 0.3, "R": 0.3},
            "status_default": 0.2, "recency_tokens": [["LAST", "PREM"], ["LAST", "PAYMENT"]],
            "recency_months": 24, "weights": {"status": 0.55, "recency": 0.35, "dates_valid": 0.10},
            "date_suffixes": ["_DT", "_DATE"], "live_cutoff": 0.7, "archive_cutoff": 0.3,
            "null_dates": ["", "00000000"], "overrides": [],
            "curve": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]}


@register_agent
class LiveArchiveAgent(BaseAgent):
    IDENTITY = agent_identity("live_archive_agent", Tier.T1, "data_quality",
                              "Scores a policy's probability of still being active", {S.LIVE_ARCHIVE_SCORES},
                              tools={"evidence.read"})

    def run(self, dictionary: list[DictionaryEntry], **_: object) -> AgentOutput:
        cfg = {**DEFAULTS, **(self.services.settings.heuristics.get("live_archive") or {})}
        summary = {"likely_live": 0, "likely_archive": 0, "review": 0, "scored": 0, "extracts": 0, "overridden": 0}
        curve = {float(x): 0 for x in cfg["curve"]}
        for ex in load_extracts(self.services, self.run_id, "source", dictionary):
            cols = {canonical_name(c): c for c in ex.header}
            status_col = cols.get(cfg["status_column"])
            if not status_col:
                continue
            last_col = next((c for k, c in cols.items()
                             if any(all(tok in k for tok in group) for group in cfg["recency_tokens"])), None)
            date_cols = [ex.idx(c) for c in ex.header if c.upper().endswith(tuple(cfg["date_suffixes"]))]
            s_idx = ex.idx(status_col)
            overrides = [o for o in cfg["overrides"] if fields_of(o["when"]) <= {h.upper() for h in ex.header}]
            l_idx = ex.idx(last_col) if last_col else None
            as_of = None
            if l_idx is not None:     # pass 1: the extract's own as-of date (latest payment seen)
                for _, row in ex.rows():
                    d = self._d(row[l_idx] if l_idx < len(row) else None, cfg)
                    if d and (as_of is None or d > as_of):
                        as_of = d

            def scores():
                for _, row in ex.rows():
                    st = (row[s_idx] if s_idx < len(row) else "").strip()
                    comps = {"status": float(cfg["status_live"].get(st, cfg["status_default"]))}
                    if l_idx is not None and as_of:
                        d = self._d(row[l_idx] if l_idx < len(row) else None, cfg)
                        months = ((as_of.year - d.year) * 12 + as_of.month - d.month) if d else None
                        comps["recency"] = 0.0 if months is None else max(0.0, 1 - months / cfg["recency_months"])
                    comps["dates_valid"] = 1.0 if all(
                        self._d(row[i] if i < len(row) else None, cfg) or not (row[i] if i < len(row) else "").strip()
                        for i in date_cols) else 0.0
                    w = {k: float(cfg["weights"][k]) for k in comps}
                    p = round(sum(comps[k] * w[k] for k in comps) / sum(w.values()), 4)
                    if overrides:
                        get = row_getter(ex.header, row)
                        for o in overrides:
                            if matches(o["when"], get) and p < float(o.get("floor", 0.9)):
                                p = float(o.get("floor", 0.9))
                                comps["override"] = p
                                summary["overridden"] += 1
                                break
                    for cut in curve:
                        if p >= cut:
                            curve[cut] += 1
                    rec = ("likely_live" if p >= cfg["live_cutoff"] else "likely_archive" if p <= cfg["archive_cutoff"]
                           else "review")
                    summary[rec] += 1
                    summary["scored"] += 1
                    yield ex.record_key(row), p, rec, {k: round(v, 4) for k, v in comps.items()}
            self.services.kc.save_live_archive_scores(self.run_id, ex.artefact.artefact_id, scores())
            summary["extracts"] += 1
        summary["threshold_curve"] = [{"cutoff": c, "live": n, "archive": summary["scored"] - n}
                                      for c, n in sorted(curve.items())]
        summary["override_rules"] = [o.get("reason") or describe(o["when"]) for o in cfg["overrides"]]
        self.note_mode(ReasoningMode.DETERMINISTIC)
        self.signal(SignalType.LIVE_ARCHIVE_SCORES, f"scored {summary['scored']} polic(ies) for live/archive status",
                    data=summary)
        return AgentOutput(items={"live_archive": [summary]})

    @staticmethod
    def _d(v: str | None, cfg: dict) -> date | None:
        if not v or v.strip() in cfg["null_dates"]:
            return None
        try:
            return parse_date(v)
        except TransformError:
            return None
