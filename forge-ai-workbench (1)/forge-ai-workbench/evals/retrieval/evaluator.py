"""Retrieval + assistant evaluation: retrieval precision/recall, citation accuracy, grounding, honest refusal."""
from __future__ import annotations

from agents.cross_cutting.assistant_agent import AssistantAgent
from evals.harness import CapabilityResult, Workspace, dataset


def evaluate(ws: Workspace) -> tuple[CapabilityResult, CapabilityResult]:
    gold = dataset("assistant_golden")
    ws.services.retrieval.refresh(ws.run_id)
    hits_ok = relevant_total = retrieved_total = 0
    answered_ok = refused_ok = cited_ok = answerable = refusable = grounded = 0
    details = []
    for q in gold["questions"]:
        hits = ws.services.retrieval.search(q["question"], project_id=ws.project_id, run_id=ws.run_id, top=3)
        out = AssistantAgent(ws.services.for_project(ws.project_id), ws.run_id).execute(question=q["question"]).items["answer"][0]
        details.append({"q": q["question"], "refused": out.refused, "citations": [c.ref for c in out.citations]})
        if q.get("refuse"):
            refusable += 1
            refused_ok += out.refused
            continue
        answerable += 1
        relevant = [h for h in hits if q["expect_text"].lower() in (h.doc.title + " " + h.doc.text).lower()]
        relevant_total += 1
        retrieved_total += len(hits)
        hits_ok += bool(relevant)
        if not out.refused:
            answered_ok += 1
            cited_ok += any(q["expect_ref"] in c.ref for c in out.citations)
            grounded += all(c.excerpt for c in out.citations)
    retrieval = CapabilityResult("retrieval", {
        "recall": round(hits_ok / max(1, relevant_total), 4),
        "precision": round(hits_ok / max(1, retrieved_total / 3), 4),
    })
    assistant = CapabilityResult("assistant", {
        "answer_rate": round(answered_ok / max(1, answerable), 4),
        "refusal_accuracy": round(refused_ok / max(1, refusable), 4),
        "citation_accuracy": round(cited_ok / max(1, answered_ok), 4),
        "grounding": round(grounded / max(1, answered_ok), 4),
    }, {"answers": details})
    return retrieval, assistant
