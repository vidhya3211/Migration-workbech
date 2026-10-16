"""Cartographer agent (T3): classifies every incoming file and routes it (mechanical work only).

Extension + content sniffing from the file-type registry. Content wins over extension when unambiguous,
because files often arrive with the wrong extension (a COBOL program landed as ``.txt``). The system
name/role come from the upload metadata - never from hardcoded names or path guesses. The extractor an
artefact is routed to comes from the agent registry (the extractor that declares it handles the type), or a
project's ``heuristics.routing`` override.

Only the first 64 KB of a file is read to classify it; line counts stream, so a multi-GB extract costs no memory.
"""
from __future__ import annotations

from pathlib import PurePosixPath

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from ingestion.decode import BinaryContent, decode_bytes
from schemas.artefact import Artefact
from schemas.common import Tier, SystemRef, SystemRole, stable_id
from schemas.signals import SignalType
from blackboard.identity import extractor_for
from tools.parsers.registry import REGISTRY, FileType, by_extension

HEAD_BYTES = 65536
NEWLINE = bytes([10])


def content_scores(text: str) -> dict[str, tuple[float, list[str]]]:
    scores: dict[str, tuple[float, list[str]]] = {}
    for ft in REGISTRY.values():
        total, why = 0.0, []
        for pattern, weight, reason in ft.sniffers:
            if pattern.search(text):
                total += weight
                why.append(reason)
        if total:
            scores[ft.artefact_type] = (min(total, 1.0), why)
    if "cobol_program" in scores and "cobol_copybook" in scores and "PROCEDURE DIVISION" in scores["cobol_program"][1]:
        del scores["cobol_copybook"]
    return scores


def sniff_binary(data: bytes, ext: str) -> tuple[str, str] | None:
    if data.startswith(b"%PDF"):
        return "document", "PDF signature"
    if data.startswith(b"PK\x03\x04"):
        head = data[:8192]
        if b"word/" in head or ext == ".docx":
            return "document", "OOXML (Word) package"
        if b"xl/" in head or ext in (".xlsx", ".xlsm"):
            return "spreadsheet", "OOXML (Excel) package"
    return None


def decide_text(ext: str, text: str, reasons: list[str]) -> tuple[str, float]:
    scores = content_scores(text)
    best_type, (best_score, best_why) = max(scores.items(), key=lambda kv: kv[1][0], default=("unknown", (0.0, [])))
    candidates: list[FileType] = by_extension(ext)
    code = [ft for ft in candidates if ft.code]
    if code:
        confirmed = sorted(((scores.get(ft.artefact_type, (0.0, []))[0], ft) for ft in code), key=lambda x: -x[0])
        top_score, top = confirmed[0]
        if top_score >= 0.3:
            reasons.append(f"extension {ext} confirmed by content: {', '.join(scores[top.artefact_type][1])}")
            return top.artefact_type, min(0.99, 0.7 + top_score * 0.3)
        if best_score >= 0.7:
            reasons.append(f"extension {ext} suggests {top.artefact_type} but content says {best_type}: {', '.join(best_why)}")
            return best_type, 0.8
        reasons.append(f"extension {ext} only; content did not confirm {top.artefact_type}")
        return top.artefact_type, 0.6
    if candidates:
        if best_score >= 0.7 and best_type != candidates[0].artefact_type:
            reasons.append(f"extension {ext} overridden by content: {', '.join(best_why)}")
            return best_type, 0.85
        reasons.append(f"extension {ext}")
        return candidates[0].artefact_type, 0.9
    if best_score >= 0.5:
        reasons.append(f"unrecognised extension {ext or '(none)'}; content: {', '.join(best_why)}")
        return best_type, 0.5 + best_score * 0.4
    reasons.append(f"unrecognised extension {ext or '(none)'} and no content signature")
    return "unknown", 0.3


@register_agent
class CartographerAgent(BaseAgent):
    IDENTITY = agent_identity("cartographer_agent", Tier.T3, "discovery", 'Classifies every incoming file', {S.ARTEFACTS_CLASSIFIED}, tools={'evidence.read'}, proposes={'artefact'})

    def run(self, staged: list[dict], **_: object) -> AgentOutput:
        artefacts: list[Artefact] = []
        routing = self.services.settings.heuristics.get("routing") or {}
        for s in staged:
            head = self.services.reader.head(s["blob_uri"], HEAD_BYTES)
            ext = PurePosixPath(s["path"]).suffix.lower()
            reasons: list[str] = []
            encoding: str | None = None
            line_count = 0
            ingest = dict(s.get("ingest") or {})
            binary = sniff_binary(head, ext)
            if binary:
                atype, conf = binary[0], 0.95
                reasons.append(f"binary signature: {binary[1]}")
            else:
                try:
                    text, encoding = decode_bytes(head)
                    if ingest.get("decoder") in ("delimited", "copybook", "fixed_width"):
                        # ingest re-encoded the staged copy as UTF-8; the original encoding is only provenance
                        encoding = "utf-8"
                        if ingest.get("encoding") not in (None, "utf-8"):
                            reasons.append(f"source encoding {ingest['encoding']} converted to UTF-8 at ingest")
                    if encoding != "utf-8":
                        reasons.append(f"decoded as {encoding}")
                    atype, conf = decide_text(ext, text, reasons)
                    line_count = self._count_lines(s["blob_uri"])
                except BinaryContent:
                    atype, conf = "unknown", 0.3
                    reasons.append("binary content with no recognised signature")
            if ingest.get("decoder") == "fixed_width":
                atype, conf = "data_extract", 0.99
                reasons.append(f"fixed-width ledger decoded with layout {ingest.get('descriptor')}"
                               + ("; trailer control totals reconciled" if (ingest.get("controls") or {}).get("reconciled")
                                  else "") + (f"; {len(ingest.get('quarantine') or [])} record(s) quarantined"
                                              if ingest.get("quarantine") else ""))
            if ingest.get("decoder") == "copybook":
                atype, conf = "data_extract", 0.99
                reasons.append(f"fixed-length EBCDIC extract decoded with copybook {ingest.get('copybook')}"
                               + ("; trailer control totals reconciled" if (ingest.get("controls") or {}).get("reconciled")
                                  else ""))
            ft = REGISTRY.get(atype)
            artefacts.append(Artefact(
                artefact_id=stable_id("A", self.run_id, s["system_role"], s["path"]), run_id=self.run_id,
                upload_id=s["upload_id"],
                system=SystemRef(system_name=s["system_name"], system_role=SystemRole(s["system_role"])),
                path=s["path"], artefact_type=atype, language=ft.language if ft else None, encoding=encoding,
                sha256=s["staged_sha256"], size_bytes=s["size_bytes"], line_count=line_count,
                confidence=round(conf, 2), reasons=reasons + ([f"PII obfuscated: {ingest['pii_obfuscated']}"]
                                                               if ingest.get("pii_obfuscated") else []),
                route=extractor_for(atype, routing), blob_uri=s["blob_uri"],
                classification=ft.classification if ft else "internal", ingest=ingest,
            ))
        self.services.kc.save_artefacts(self.name, artefacts)
        self.services.invalidate(self.run_id)
        by_type: dict[str, int] = {}
        for a in artefacts:
            by_type[a.artefact_type] = by_type.get(a.artefact_type, 0) + 1
        self.signal(SignalType.ARTEFACTS_CLASSIFIED,
                    f"classified {len(artefacts)} file(s): " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())),
                    item_refs=[a.artefact_id for a in artefacts], data={"by_type": by_type})
        return AgentOutput(items={"artefacts": artefacts})

    def _count_lines(self, uri: str) -> int:
        n, last = 0, b""
        with self.services.reader.open(uri) as fh:
            for buf in iter(lambda: fh.read(1024 * 1024), b""):
                n += buf.count(NEWLINE)
                last = buf
        return n + (1 if last and not last.endswith(NEWLINE) else 0)
