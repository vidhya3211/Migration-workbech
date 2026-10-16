"""Volume: hundreds of thousands of rows stream through staging, profiling, duplicate detection and the
reconciliation join without being held in memory. Row count via AIWB_VOLUME_ROWS (default 300 000)."""
import io
import os
import time
import tracemalloc

from schemas.common import new_id

ROWS = int(os.environ.get("AIWB_VOLUME_ROWS", "300000"))


class CsvStream(io.RawIOBase):
    """A generated CSV extract, produced lazily - the test never materialises the file."""

    def __init__(self, n: int, *, target: bool = False) -> None:
        self.n, self.target, self.i, self.buf = n, target, -1, b""

    def readable(self) -> bool:
        return True

    def _line(self, i: int) -> bytes:
        if i < 0:
            return b"POLICY_NO,STATUS,PREMIUM,START_DATE\n"
        premium = f"{100 + i % 900}.{i % 100:02d}"
        if self.target and i % 1000 == 7:
            premium = f"{101 + i % 900}.{i % 100:02d}"          # one break per thousand records
        status = "LAPSED" if i % 97 == 0 else "ACTIVE"
        return f"P{i:09d},{status},{premium},2019-{1 + i % 12:02d}-{1 + i % 28:02d}\n".encode()

    def readinto(self, b) -> int:
        while len(self.buf) < len(b) and self.i < self.n:
            self.buf += self._line(self.i)
            self.i += 1
        k = min(len(b), len(self.buf))
        b[:k], self.buf = self.buf[:k], self.buf[k:]
        return k


def test_large_extracts_stream_through_staging_and_reconciliation(ws):
    kc, staging = ws.services.kc, ws.services.staging
    pid = ws.project("Volume estate")
    run_id = new_id("RUN")
    kc.create_run(run_id, "volume-test", "legacy", "target", project_id=pid, pipeline={"name": "default"},
                  pipeline_hash="volume", steps_total=1)
    tracemalloc.start()
    t0 = time.perf_counter()
    common = dict(project_id=pid, run_id=run_id, encoding="utf-8", delimiters=[",", "|", "\t"])
    src = staging.stage_delimited(artefact_id="ART-SRC", system_name="legacy", system_role="source",
                                  path="extracts/policy.csv", stream=io.BufferedReader(CsvStream(ROWS)), **common)
    tgt = staging.stage_delimited(artefact_id="ART-TGT", system_name="target", system_role="target",
                                  path="extracts/policy.csv", stream=io.BufferedReader(CsvStream(ROWS, target=True)), **common)
    staged = time.perf_counter() - t0
    assert src.row_count == ROWS and tgt.row_count == ROWS

    stats = staging.column_stats("ART-SRC", 1)
    assert stats["rows"] == ROWS and stats["distinct"] == 2
    assert staging.duplicates("ART-SRC", 0) == []

    staging.load_recon_keys(run_id, "PAIR-1", ((f"P{i:09d}", i + 2) for i in range(ROWS)))
    matched = mismatched = 0
    for _key, _s_line, s_vals, t_line, t_vals in staging.recon_source_join(run_id, "PAIR-1", "ART-SRC", "ART-TGT", 0):
        assert t_line is not None
        matched += 1
        mismatched += s_vals[2] != t_vals[2]
    assert matched == ROWS
    assert mismatched == sum(1 for i in range(ROWS) if i % 1000 == 7)
    assert list(staging.recon_unexpected(run_id, "PAIR-1", "ART-TGT", 0)) == []
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    total = time.perf_counter() - t0
    print(f"\n{ROWS:,} rows x2: staged in {staged:.1f}s, reconciled in {total - staged:.1f}s, "
          f"peak Python memory {peak / 2**20:.0f} MiB")
    assert peak < 200 * 2**20, "streaming must not hold the extracts in memory"
    staging.drop_recon_keys(run_id)
    staging.drop_run(run_id)
