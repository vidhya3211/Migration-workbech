"""Reasoning evaluation: differential discovery (deterministic engine) and LLM-path integrity.

The LLM path is evaluated with a scripted provider that deliberately returns a hallucinated citation, an
unsupported figure and malformed JSON - the guardrails must stop every one. Consistency checks that the
same question yields the same answer.
"""
from __future__ import annotations

from db import models as m
from evals.harness import CapabilityResult, Workspace, dataset


def evaluate_differentials(ws: Workspace) -> CapabilityResult:
    gold = dataset("differential_golden")
    rows = ws.services.kc.rows(m.Differential, ws.run_id)
    got = {(d.canonical_name, d.difference_type) for d in rows}
    names = {d.canonical_name for d in rows}
    exp = gold["expected"]
    critical = [e for e in exp if e["critical"]]
    hit = [e for e in exp if (e["canonical_name"], e["type"]) in got]
    name_hit = [e for e in exp if e["canonical_name"] in names]
    false_flags = sorted(names & set(gold["must_not_flag"]))
    exposure_ok = sum(1 for e in exp if "exposure" in e and any(d.canonical_name == e["canonical_name"] and
                                                                  d.financial_exposure == e["exposure"] for d in rows))
    boundary_ok = 0
    for e in exp:
        if "divergent_input" in e:
            d = next((d for d in rows if d.canonical_name == e["canonical_name"]), None)
            vecs = (d.body.get("divergence") or []) if d else []
            boundary_ok += any(all(v.get("inputs", {}).get(k) == val for k, val in e["divergent_input"].items())
                               for v in vecs)
    proven = sum(1 for d in rows if d.body.get("divergence") or d.difference_type.startswith("missing")
                 or d.difference_type == "document_conflict")
    return CapabilityResult("differentials", {
        "critical_recall": round(sum(1 for e in critical if e["canonical_name"] in names) / len(critical), 4),
        "recall": round(len(name_hit) / len(exp), 4),
        "type_accuracy": round(len(hit) / max(1, len(name_hit)), 4),
        "precision": round(1 - len(false_flags) / max(1, len(rows)), 4),
        "exposure_accuracy": round(exposure_ok / max(1, sum(1 for e in exp if "exposure" in e)), 4),
        "boundary_detection": float(boundary_ok),
        "proven_share": round(proven / max(1, len(rows)), 4),
    }, {"found": sorted(map(list, got)), "false_flags": false_flags})


def evaluate_llm(ws: Workspace) -> CapabilityResult:
    """LLM hallucination / unsupported-claim / citation / structured-output / consistency checks (scripted)."""
    from context.headroom.policy import ContentClass
    from guardrails.output.policies import CitedAnswer
    from guardrails.pipeline import GuardrailContext, GuardrailViolation, Stage
    from reasoning.llm.gateway import Evidence, LLMGateway
    from reasoning.llm.providers import ScriptedProvider
    from agents.cross_cutting.assistant_agent import Composed

    evidence_text = "rule:R-1 ADMIN FEE certified expression 25.00 certified by A. Actuary"
    scripts = {
        "hallucinated_citation": '{"answer": "The admin fee is 25.00.", "citations": ["rule:R-999"], "refused": false}',
        "unsupported_figure": '{"answer": "The admin fee is 30.00.", "citations": ["rule:R-1"], "refused": false}',
        "grounded": '{"answer": "The certified admin fee is 25.00.", "citations": ["rule:R-1"], "refused": false}',
        "malformed": 'The admin fee is probably 25 - no JSON here',
    }
    outcomes = {}
    for case, text in scripts.items():
        provider = ScriptedProvider({"compose_cited_answer": text})
        gw = LLMGateway(ws.settings, provider, ws.services.headroom, ws.services.guardrails)
        res = gw.call(agent="assistant_agent", run_id="EVAL", purpose="compose_cited_answer",
                      variables={"question": "What is the admin fee?"},
                      evidence=[Evidence("rule:R-1", evidence_text, ContentClass.SEARCH_RESULT)], output_model=Composed)
        if res.parsed is None:
            outcomes[case] = "rejected_schema"
            continue
        try:
            ws.services.guardrails.run(Stage.OUTPUT_SCHEMA, CitedAnswer(res.parsed.answer, res.parsed.citations,
                                                                        {"rule:R-1": evidence_text}),
                                       GuardrailContext("EVAL", "assistant_agent"),
                                       only={"citation_integrity", "unsupported_claims"})
            outcomes[case] = "accepted"
        except GuardrailViolation:
            outcomes[case] = "blocked"
    bad = ["hallucinated_citation", "unsupported_figure"]
    consistency = []
    for _ in range(2):
        provider = ScriptedProvider({"compose_cited_answer": scripts["grounded"]})
        gw = LLMGateway(ws.settings, provider, ws.services.headroom, ws.services.guardrails)
        consistency.append(gw.call(agent="assistant_agent", run_id="EVAL", purpose="compose_cited_answer",
                                   variables={"question": "q"}, evidence=[Evidence("rule:R-1", evidence_text,
                                                                                   ContentClass.SEARCH_RESULT)],
                                   output_model=Composed).parsed)
    return CapabilityResult("llm", {
        "hallucination_block_rate": sum(outcomes[c] == "blocked" for c in bad) / len(bad),
        "unsupported_claim_rate": float(outcomes["unsupported_figure"] == "accepted"),
        "citation_accuracy": float(outcomes["grounded"] == "accepted"),
        "structured_output_validity": float(outcomes["malformed"] == "rejected_schema"),
        "reasoning_consistency": float(consistency[0] == consistency[1]),
    }, {"outcomes": outcomes})
