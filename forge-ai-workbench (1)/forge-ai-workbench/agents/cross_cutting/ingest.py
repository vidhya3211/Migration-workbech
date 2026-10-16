"""Ingest agent (T3): the only agent allowed to write new estate data in. Halts on any count/total mismatch.

For each validated upload it streams the stored bundle member by member (bounded memory for multi-GB
bundles) and stages each file into the evidence store under ``<project>/<run>/<role>/<path>``:

* every member is re-hashed while it is copied and must match the upload manifest byte-for-byte;
* delimited data extracts are PII-tokenised row by row before they land;
* fixed-length EBCDIC extracts shipped with their copybook are decoded to delimited text (with the trailer
  control totals recomputed from the bytes - a mismatch halts the run) and tokenised the same way.

``stage_data`` (the second step this agent owns) loads classified data extracts into PostgreSQL with COPY.
"""
from __future__ import annotations

import csv
import io
import tempfile
from pathlib import PurePosixPath

from agents.base import AgentOutput, BaseAgent
from blackboard.identity import agent_identity, register_agent
from ingestion.decode import BinaryContent, decode_bytes
from ingestion.fixed_width import decode_fixed_width, is_fixed_width
from ingestion.mainframe import ControlTotalMismatch, DecodeError, decode_stream, find_companions, load_descriptor
from ingestion.pii import looks_like_delimited_extract, obfuscate_delimited, obfuscate_rows
from ingestion.upload import HashingReader, inspect_bundle, open_zip
from schemas.common import Tier
from schemas.signals import SignalLevel, SignalType
from schemas.signals import SignalType as S
from staging.extracts import StagingError

DATA_EXTENSIONS = (".csv", ".dat", ".psv", ".tsv")
RECORD_EXTENSIONS = (".dat", ".txt", ".fw", ".fix")     # decoded only when a copybook / layout descriptor is beside them
SPOOL_BYTES = 64 * 1024 * 1024


class IngestHalted(RuntimeError):
    pass


@register_agent
class IngestAgent(BaseAgent):
    IDENTITY = agent_identity("ingest_agent", Tier.T3, "cross_cutting", "Only agent allowed to write new estate data in",
                              {S.INGEST_MANIFEST}, tools={"evidence.write"}, proposes={"artefact"})

    def run(self, upload_ids: list[str], project_id: str, **_: object) -> AgentOutput:
        settings = self.services.settings
        scope = f"{project_id}/{self.run_id}"
        staged: list[dict] = []
        decoded, pii_files = 0, []
        for upload_id in upload_ids:
            row = self.services.kc.get_upload(upload_id)
            if row is None or row.project_id != project_id:
                raise IngestHalted(f"upload {upload_id} not found in this project")
            if row.status not in ("ready", "consumed"):
                raise IngestHalted(f"upload {upload_id} is {row.status}; only validated uploads can be ingested")
            expected = self.services.kc.upload_files(upload_id)
            role, system = row.system_role, row.system_name
            with self.services.objects.open(row.stored_uri) as fh:
                zf = open_zip(fh)
                members = inspect_bundle(zf, row.bundle_bytes, settings.ingestion)
                if len(members) != len(expected):
                    raise IngestHalted(f"upload {upload_id}: count mismatch - manifest {len(expected)}, "
                                       f"bundle {len(members)}")
                paths = [m.path for m in members]
                companions = {}
                for mem in members:
                    if not mem.path.lower().endswith(RECORD_EXTENSIONS):
                        continue
                    cpy, desc = find_companions(mem.path, paths)
                    if not mem.path.lower().endswith(".dat"):
                        cpy = None       # a .txt beside a copybook may be the COBOL program itself: never decode it
                        if desc is None:
                            continue
                    if cpy or desc:
                        companions[mem.path] = (cpy, desc)
                small = {p: zf.read(next(x.info for x in members if x.path == p))
                         for cp in companions.values() for p in cp if p}
                for mem in members:
                    want = expected.get(mem.path)
                    if want is None:
                        raise IngestHalted(f"upload {upload_id}: {mem.path} is not in the manifest")
                    with zf.open(mem.info) as raw:
                        hr = HashingReader(raw, limit=mem.size_bytes)
                        item = self._stage_member(scope, role, mem.path, hr, companions.get(mem.path), small)
                        digest = hr.finish(settings.ingestion.stream_buffer_bytes)
                    if digest != want[1] or hr.n != want[0]:
                        raise IngestHalted(f"upload {upload_id}: {mem.path} differs from the manifest (sha256/size)")
                    self.ledger.record_tool_call()
                    decoded += item["ingest"].get("decoder") in ("copybook", "fixed_width")
                    if item["ingest"].get("pii_obfuscated"):
                        pii_files.append(mem.path)
                    staged.append({"upload_id": upload_id, "path": mem.path, "system_name": system,
                                   "system_role": role, "upload_sha256": digest, **item})
            if sum(1 for s in staged if s["upload_id"] == upload_id) != len(expected):
                raise IngestHalted(f"upload {upload_id}: staged count differs from manifest")
            self.services.kc.attach_upload(upload_id, self.run_id)
        self.signal(SignalType.INGEST_MANIFEST,
                    f"staged {len(staged)} file(s) from {len(upload_ids)} upload(s); {decoded} mainframe extract(s) "
                    f"decoded (copybook / fixed-width layout); PII tokenised in {len(pii_files)}",
                    data={"files": len(staged), "uploads": upload_ids, "pii_obfuscated_files": pii_files[:200],
                          "mainframe_decoded": decoded})
        return AgentOutput(items={"staged": staged})

    # ------------------------------------------------------------------ one member
    def _stage_member(self, scope: str, role: str, path: str, stream: HashingReader,
                      companions: tuple[str | None, str | None] | None, small: dict[str, bytes]) -> dict:
        st = self.services.settings
        rel = f"{role}/{path}"
        if companions and (companions[0] or companions[1]):
            return self._stage_mainframe(scope, rel, path, stream, companions, small)
        if path.lower().endswith(DATA_EXTENSIONS):
            buffered = io.BufferedReader(stream, buffer_size=st.ingestion.stream_buffer_bytes)
            head = buffered.peek(65536)[:65536]
            try:
                text, enc = decode_bytes(head)
            except BinaryContent:
                text, enc = None, None
            if text is not None and looks_like_delimited_extract(text, st.ingestion.csv_delimiters):
                with tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES) as tmp:
                    out = io.TextIOWrapper(tmp, encoding="utf-8", newline="")
                    src = io.TextIOWrapper(buffered, encoding="utf-8-sig" if enc == "utf-8" else enc, newline="")
                    obf, conf, delim = obfuscate_delimited(src, out, st.ingestion.pii, st.environment,
                                                           st.ingestion.csv_delimiters)
                    src.detach()          # never let a wrapper's finaliser close the hashing stream
                    buffered.detach()
                    out.flush()
                    out.detach()
                    tmp.seek(0)
                    uri, size, sha = self.services.store.put_stream(scope, rel, tmp)
                return {"blob_uri": uri, "staged_sha256": sha, "size_bytes": size,
                        "ingest": {"decoder": "delimited", "encoding": enc, "delimiter": delim,
                                   "pii_obfuscated": obf, "pii_confidential": conf}}
            uri, size, sha = self.services.store.put_stream(scope, rel, buffered)
            buffered.detach()
            return {"blob_uri": uri, "staged_sha256": sha, "size_bytes": size, "ingest": {}}
        uri, size, sha = self.services.store.put_stream(scope, rel, stream)
        return {"blob_uri": uri, "staged_sha256": sha, "size_bytes": size, "ingest": {}}

    def _stage_mainframe(self, scope: str, rel: str, path: str, stream: HashingReader,
                         companions: tuple[str | None, str | None], small: dict[str, bytes]) -> dict:
        st = self.services.settings
        cpy, desc = companions
        try:
            descriptor = load_descriptor(small[desc]) if desc else None
            buffered = io.BufferedReader(stream, buffer_size=1024 * 1024)
            if cpy is None:
                if not is_fixed_width(descriptor):
                    raise DecodeError("a descriptor without a copybook must declare format fixed_width and fields")
                columns, rows, result = decode_fixed_width(buffered, descriptor)
            else:
                copybook = small[cpy].decode("utf-8", errors="replace")
                columns, rows, result = decode_stream(buffered, copybook, descriptor)
            obf_idx, conf, safe_rows = obfuscate_rows(columns, (vals for _, vals in rows), st.ingestion.pii,
                                                      st.environment)
            with tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES) as tmp:
                out = io.TextIOWrapper(tmp, encoding="utf-8", newline="")
                w = csv.writer(out, lineterminator="\n")
                w.writerow(columns)
                for r in safe_rows:
                    w.writerow(r)
                out.flush()
                out.detach()
                tmp.seek(0)
                uri, size, sha = self.services.store.put_stream(scope, rel, tmp)
            buffered.detach()
        except ControlTotalMismatch as exc:
            self.signal(SignalType.INGEST_MANIFEST, f"{path}: {exc}", level=SignalLevel.ERROR)
            raise IngestHalted(f"{path}: control totals do not reconcile - ingestion halted") from exc
        except DecodeError as exc:
            raise IngestHalted(f"{path}: could not decode with {cpy or desc}: {exc}") from exc
        quarantined = getattr(result, "quarantine", [])
        if result.rejected:
            self.signal(SignalType.INGEST_MANIFEST, f"{path}: {result.rejected} record(s) quarantined at decode "
                        "(see the artefact's ingest facts)", level=SignalLevel.WARN,
                        data={"path": path, "quarantined": quarantined[:20]})
        return {"blob_uri": uri, "staged_sha256": sha, "size_bytes": size,
                "ingest": {"decoder": "copybook" if cpy else "fixed_width", "copybook": cpy, "descriptor": desc,
                           "encoding": "cp037" if cpy else (descriptor or {}).get("encoding", "utf-8"),
                           "quarantine": quarantined,
                           "controls": result.controls, "records": result.records, "rejected": result.rejected,
                           "pii_obfuscated": [columns[i] for i in obf_idx], "pii_confidential": conf,
                           "source_format": PurePosixPath(path).suffix.lower()}}

    # ------------------------------------------------------------------ stage_data (second step)
    def stage(self, project_id: str, artefacts: list) -> AgentOutput:
        """Load every classified data extract into PostgreSQL (COPY) so Stage-5 agents can stream it."""
        st = self.services.settings
        staged, rows, failed = [], 0, []
        for art in artefacts:
            if art.artefact_type != "data_extract":
                continue
            try:
                with self.services.reader.open(art.blob_uri) as fh:
                    info = self.services.staging.stage_delimited(
                        artefact_id=art.artefact_id, project_id=project_id, run_id=self.run_id,
                        system_name=art.system.system_name, system_role=art.system.system_role.value, path=art.path,
                        stream=fh, encoding=art.encoding or "utf-8", delimiters=st.ingestion.csv_delimiters)
            except (StagingError, UnicodeDecodeError, csv.Error) as exc:
                failed.append({"artefact_id": art.artefact_id, "path": art.path, "error": str(exc)[:300]})
                continue
            staged.append({"artefact_id": art.artefact_id, "rows": info.row_count, "columns": len(info.columns)})
            rows += info.row_count
            self.ledger.record_tool_call()
        changes = self._apply_change_data(staged)
        level = SignalLevel.WARN if failed or any(c.get("conflicts") for c in changes) else SignalLevel.INFO
        if changes:
            self.signal(SignalType.INGEST_MANIFEST, "change data applied: " + "; ".join(
                f"{c['changes']} -> {c['base']}: {c.get('insert', 0)} insert, {c.get('update', 0)} update, "
                f"{c.get('delete', 0)} delete, {c.get('conflicts', 0)} conflict(s)" if "error" not in c
                else f"{c['changes']}: {c['error']}" for c in changes), level=level, data={"change_data": changes})
            self.services.kc.merge_run_summary(self.run_id, {"change_data": changes})
        self.signal(SignalType.INGEST_MANIFEST, f"staged {len(staged)} data extract(s), {rows} row(s) into "
                    f"PostgreSQL" + (f"; {len(failed)} could not be staged" if failed else ""), level=level,
                    data={"extracts": staged[:200], "rows": rows, "failed": failed[:50]})
        return AgentOutput(items={"staged_extracts": staged, "failed": failed, "change_data": changes},
                           summary={"rows": rows})

    def _apply_change_data(self, staged: list[dict]) -> list[dict]:
        """Apply each configured CDC delta file to its base extract, then drop the delta from staging (the base
        now holds the book as of the last change)."""
        from staging.cdc import apply_changes
        specs = self.services.settings.heuristics.get("change_data") or []
        if not specs:
            return []
        tables = [self.services.staging.table(s["artefact_id"]) for s in staged]
        tables = [t for t in tables if t is not None]

        def pick(sel: dict):
            for t in tables:
                if sel.get("path_contains") and str(sel["path_contains"]).lower() in t.path.lower():
                    return t
                if sel.get("entity") and PurePosixPath(t.path).stem.upper() == str(sel["entity"]).upper():
                    return t
            return None
        out = []
        for spec in specs:
            ch, base = pick(spec.get("changes") or {}), pick(spec.get("base") or {})
            if ch is None or base is None:
                continue
            try:
                out.append(apply_changes(self.services.staging, base, ch, spec))
                self.services.staging.drop_extract(ch.artefact_id)
            except ValueError as exc:
                out.append({"changes": ch.path, "base": base.path, "error": str(exc)})
        return out
