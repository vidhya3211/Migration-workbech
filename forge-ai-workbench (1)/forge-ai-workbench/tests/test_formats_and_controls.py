"""Source formats and the reconciliation checks a record-by-record comparison cannot make: fixed-width ledgers,
CDC delta files, derived values, cross-target drift, control totals (e.g. CASS), lifted rate tables, the
product x domain coverage matrix and date-format-tolerant record linkage."""
import io
import json

import pytest

from agents.discovery.common import evidence_for, table_rule
from agents.discovery.differential_agent import DifferentialAgent
from agents.reconciliation.control_agent import ControlReconcilerAgent
from config.settings import load_settings
from db import models as m
from evals.harness import Workspace
from ingestion.fixed_width import decode_fixed_width
from ingestion.mainframe import ControlTotalMismatch
from schemas.artefact import Artefact
from schemas.common import SystemRef, SystemRole, new_id
from schemas.provenance import CodeLocator
from schemas.rules import NormalisedRule, RuleKind
from services.coverage import coverage
from staging.cdc import apply_changes
from tools.parsers.cobol import normalise, parse_data_items, split_sentences
from tools.parsers.tables import Table, cobol_tables, php_tables


# ------------------------------------------------------------------ fixed width
LAYOUT = {"format": "fixed_width", "encoding": "utf-8", "skip_lines": 1,
          "fields": [{"name": "ACCOUNT", "start": 1, "length": 6},
                     {"name": "BALANCE", "start": 7, "length": 9, "type": "number", "decimals": 2},
                     {"name": "OPENED", "start": 16, "length": 8, "type": "date", "date_format": "%d%m%Y"}],
          "trailer": {"starts_with": "TRL", "count": {"start": 4, "length": 4},
                      "sums": [{"field": "BALANCE", "start": 8, "length": 10, "decimals": 2}]}}


def _fw(lines: list[str]):
    cols, rows, result = decode_fixed_width(io.BytesIO(("HEADER\n" + "\n".join(lines) + "\n").encode()), LAYOUT)
    return cols, list(rows), result


def test_fixed_width_decodes_and_reconciles_its_trailer():
    cols, rows, res = _fw(["AC0001000012550010120201", "AC0002-00000100150620190", "TRL00020000012450"])
    assert cols == ["ACCOUNT", "BALANCE", "OPENED"]
    assert rows[0][1] == ["AC0001", "125.50", "2020-01-01"] and rows[1][1][1] == "-1.00"
    assert res.controls["reconciled"] is True


def test_fixed_width_quarantines_bad_records_and_halts_on_lost_data():
    _, rows, res = _fw(["AC0001000012550010120201", "AC0002XXXXXXXXX15062019", "TRL00020000012550"])
    assert len(rows) == 1 and res.rejected == 1 and res.quarantine[0]["record"] == 3
    assert res.controls["reconciled"] and res.controls["rejected_records"] == 1
    # the trailer total includes the quarantined record's amount: reported with it, not halted
    _, _, res = _fw(["AC0001000012550010120201", "AC0002XXXXXXXXX15062019", "TRL00020000012650"])
    assert res.controls["reconciled"] is False and "quarantined" in res.controls["note"]
    with pytest.raises(ControlTotalMismatch):
        _fw(["AC0001000012550010120201", "TRL00020000012550"])


# ------------------------------------------------------------------ staged helpers
def _run(ws: Workspace) -> tuple[str, str]:
    pid = ws.project(f"Formats {new_id('X')[-6:]}")
    rid = new_id("RUN")
    ws.services.kc.create_run(rid, "test", "a", "b", project_id=pid, pipeline={"name": "default"},
                              pipeline_hash="t", steps_total=1)
    return pid, rid


def _stage(ws, pid, rid, path, csv_text, role="source"):
    """Stage an extract and register its artefact, as ingest + cartographer do in a run."""
    aid = new_id("ART")
    st = ws.services.staging.stage_delimited(
        artefact_id=aid, project_id=pid, run_id=rid, system_name=role, system_role=role, path=path,
        stream=io.BytesIO(csv_text.encode()), encoding="utf-8", delimiters=[","])
    ws.services.kc.save_artefacts("cartographer_agent", [Artefact(
        artefact_id=aid, run_id=rid, upload_id="U", system=SystemRef(system_name=role, system_role=SystemRole(role)),
        path=path, artefact_type="data_extract", sha256="0" * 64, size_bytes=len(csv_text), confidence=1.0,
        blob_uri="obj://x/y", classification="confidential")])
    ws.services.shared.artefacts.pop(rid, None)
    return st


def test_cdc_changes_apply_in_sequence_and_conflicts_are_kept(ws):
    pid, rid = _run(ws)
    base = _stage(ws, pid, rid, "data/member_base.csv", "MEMBER_ID,NAME,STATUS\n1,Ann,A\n2,Bob,A\n3,Cal,A\n")
    changes = _stage(ws, pid, rid, "data/member_cdc.csv",
                     "LSN,OP,MEMBER_ID,STATUS,OLD_STATUS\n"
                     "10,U,1,L,A\n"        # ok
                     "11,D,2,,\n"          # ok
                     "12,I,4,A,\n"         # ok (new member)
                     "13,U,9,L,A\n"        # update of a missing record
                     "14,I,3,A,\n"         # insert of an existing record
                     "15,U,3,L,X\n"        # old value does not match
                     "9,U,1,X,L\n")        # applies first (sequence order) - then 10 finds STATUS=X, not A
    res = apply_changes(ws.services.staging, base, changes, {"key_field": "MEMBER_ID", "op_field": "OP",
                                                             "sequence_field": "LSN", "old_prefix": "OLD_"})
    rows = {r[0]: r for _, r in ws.services.staging.iter_rows(base.artefact_id)}
    reasons = sorted(c["reason"].split(" for ")[0] for c in res["conflict_details"])
    assert res["conflicts"] == 4 and res["update"] == 1 and res["delete"] == 1 and res["insert"] == 1
    assert "2" not in rows and rows["4"][2] == "A" and rows["1"][2] == "L" and rows["3"][2] == "A"
    assert reasons == ["insert of a record that already exists", "old value differs from the current record",
                       "old value differs from the current record", "update of a record that does not exist"]
    assert res["rows_after"] == 3


def test_derived_values_cross_target_and_control_totals(ws):
    pid, rid = _run(ws)
    _stage(ws, pid, rid, "data/holdings.csv", "POLICY,FUND,UNITS\nP1,F1,100\nP1,F2,10\nP2,F1,50\nP3,F9,5\n")
    _stage(ws, pid, rid, "data/prices.csv",
           "FUND,PRICE_DATE,BID\nF1,2026-09-01,1.00\nF1,2026-09-02,1.10\nF2,02/09/2026,5.00\n")
    _stage(ws, pid, rid, "data/fund_values.csv",
           "POLICY,VAL_DATE,FUND_VALUE\nP1,2026-09-02,160.00\nP2,2026-09-01,55.00\nP3,2026-09-02,1.00\n", "target")
    _stage(ws, pid, rid, "data/platform_b.csv", "POLICY,FUND_VALUE\nP1,160.00\nP2,50.00\n", "target")
    _stage(ws, pid, rid, "data/client_money.csv", "ACCOUNT,BALANCE,FUND\nA1,100.00,F1\nA2,50.25,F1\nA3,10.00,F2\n")
    _stage(ws, pid, rid, "data/custodian.csv", "FUND,HOLDING\nF1,150.25\nF2,12.00\n")
    cfg = {"derived": [{"name": "fund value", "target": {"path_contains": "fund_values", "key": "POLICY",
                                                         "date": "VAL_DATE", "field": "FUND_VALUE"},
                        "holdings": {"path_contains": "holdings", "key": "POLICY", "instrument": "FUND", "quantity": "UNITS"},
                        "prices": {"path_contains": "prices", "instrument": "FUND", "date": "PRICE_DATE", "price": "BID"}}],
           "cross_target": [{"name": "platform drift", "left": {"path_contains": "fund_values", "key": "POLICY"},
                             "right": {"path_contains": "platform_b", "key": "POLICY"},
                             "fields": [["FUND_VALUE", "FUND_VALUE"]]}],
           "controls": [{"name": "client money vs custodian", "regime": "CASS",
                         "left": {"path_contains": "client_money", "sum": "BALANCE", "group_by": "FUND"},
                         "right": {"path_contains": "custodian", "sum": "HOLDING", "group_by": "FUND"}}]}
    ws.services.settings.heuristics["reconciliation"] = cfg
    out = ControlReconcilerAgent(ws.services, rid).execute(dictionary=[])
    breaks = {(b.break_type, b.record_key): b for b in out.items["breaks"]}
    # P1 at 2026-09-02: 100 x 1.10 + 10 x 5.00 = 160.00 (agrees); P2 at 2026-09-01: 50 x 1.00 = 50.00 (loaded 55)
    assert ("derived_value_mismatch", "P1") not in breaks
    assert breaks[("derived_value_mismatch", "P2")].expected_value == "50.00"
    assert ("derived_value_unpriced", "P3") in breaks
    assert breaks[("cross_target_drift", "P2")].target_value == "50.00"
    control = out.items["controls"][0]["controls"][0]
    assert control["reconciled"] is False and [x["group"] for x in control["mismatches"]] == ["F2"]
    assert breaks[("control_mismatch", "F2")].note.startswith("CASS")


# ------------------------------------------------------------------ rate tables
COBOL = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. T.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  RATE-VALUES.
           05  FILLER  PIC X(6) VALUE 'A01150'.
           05  FILLER  PIC X(6) VALUE 'B02175'.
           05  FILLER  PIC X(6) VALUE 'C03200'.
       01  RATE-TABLE REDEFINES RATE-VALUES.
           05  RATE-ENTRY OCCURS 3 TIMES.
               10  RATE-CODE  PIC X(3).
               10  RATE-PCT   PIC 9V99.
       PROCEDURE DIVISION.
           STOP RUN.
"""


def _art(role: SystemRole, path: str) -> Artefact:
    return Artefact(artefact_id=new_id("ART"), run_id="R", upload_id="U", system=SystemRef(system_name=role.value,
                    system_role=role), path=path, artefact_type="code", sha256="0" * 64, size_bytes=1, confidence=1.0,
                    blob_uri="obj://x/y")


def test_rate_tables_are_lifted_and_compared_entry_by_entry(ws):
    lines, _ = normalise(COBOL)
    items, _ = parse_data_items(split_sentences(lines), "T")
    src_t = cobol_tables(items)[0]
    tgt_t = php_tables("<?php\nclass Rates {\n  const RATE_PCT = ['A01' => 1.50, 'B02' => 1.80];\n}\n")[0]
    assert src_t.entries == {"A01": "1.5", "B02": "1.75", "C03": "2"} and tgt_t.index_by == "key"
    normalised = []
    for role, t, text, path in ((SystemRole.SOURCE, src_t, COBOL, "src/T.cbl"),
                                (SystemRole.TARGET, tgt_t, "<?php\nclass Rates {\n  const RATE_PCT = ['A01' => 1.50, 'B02' => 1.80];\n}\n", "tgt/Rates.php")):
        art = _art(role, path)
        c = table_rule(agent="cobol_agent", tier="T1", art=art, table=t, lines=text.split("\n"))
        normalised.append(NormalisedRule(rule_id=c.rule_id, canonical_name="RATE_PCT", domain="charges",
                                         kind=RuleKind.TABLE, system=art.system, source_kind="code",
                                         expression=c.expression, resolved_expression=c.expression, confidence=0.9,
                                         evidence=c.evidence, agent=c.agent))
    _, rid = _run(ws)
    diffs = DifferentialAgent(ws.services, rid)._table_differences(normalised)
    assert len(diffs) == 1
    d = diffs[0]
    assert d.difference_type.value == "value_mismatch" and "B02" in d.explanation and "C03" in d.explanation
    assert {v.inputs["key"] for v in d.divergence} == {"B02", "C03"}


# ------------------------------------------------------------------ coverage + linkage
def test_coverage_matrix_by_product(golden):
    settings = load_settings(overrides={"heuristics.coverage": {"product_field": "PAY_FREQ"}})
    cov = coverage(golden.services.kc, golden.services.staging, golden.run_id, settings)
    assert {p["product"] for p in cov["products"]} == {"A", "M"}
    states = {c["state"] for row in cov["matrix"] for c in row["cells"].values()}
    assert states and states <= {"certified", "differs", "source_only", "target_only", "partly_certified",
                                 "candidate", "none"}
    assert all(set(row["cells"]) == {"A", "M"} for row in cov["matrix"])


def test_birth_dates_in_different_formats_share_a_block(ws):
    pid, rid = _run(ws)
    a = _stage(ws, pid, rid, "data/crm.csv", "ID,DOB\nX1,1990-04-12\n")
    b = _stage(ws, pid, rid, "data/admin.csv", "ID,BIRTH\nY1,12/04/1990\n")
    blocks = list(ws.services.staging.blocks_multi([(a.artefact_id, [1]), (b.artefact_id, [1])], 50, {0}))
    assert len(blocks) == 1 and blocks[0][0] == "19900412" and len(blocks[0][1]) == 2


def test_table_text_entries_compare_exactly_and_numbers_by_value(ws):
    rules = []
    for role, entries in ((SystemRole.SOURCE, {"A": "GOLD", "B": "1.5"}), (SystemRole.TARGET, {"A": "SILVER", "B": "1.50"})):
        art = _art(role, f"{role.value}/rates.php")
        text = "<?php\n$TIER = ['A' => 'x', 'B' => 1];\n"
        c = table_rule(agent="php_agent", tier="T2", art=art,
                       table=Table("TIER", entries, "key", 2, 2), lines=text.split("\n"))
        rules.append(NormalisedRule(rule_id=c.rule_id, canonical_name="TIER", domain="general", kind=RuleKind.TABLE,
                                    system=art.system, source_kind="code", expression=c.expression,
                                    resolved_expression=c.expression, confidence=0.9, evidence=c.evidence, agent=c.agent))
    _, rid = _run(ws)
    d = DifferentialAgent(ws.services, rid)._table_differences(rules)[0]
    assert [v.inputs["key"] for v in d.divergence] == ["A"]


def test_dated_holdings_and_month_first_dates(ws):
    """Units bought between two strikes are priced at the right date; 09/02/2026 means 2 September here."""
    pid, rid = _run(ws)
    _stage(ws, pid, rid, "data/holdings.csv",
           "POLICY,FUND,UNITS,AS_OF\nP1,F1,100,09/01/2026\nP1,F1,150,09/02/2026\n")
    _stage(ws, pid, rid, "data/prices.csv", "FUND,PRICE_DATE,BID\nF1,09/01/2026,1.00\nF1,09/02/2026,1.10\n")
    _stage(ws, pid, rid, "data/fund_values.csv",
           "POLICY,VAL_DATE,FUND_VALUE\nP1,2026-09-01,100.00\nP1,2026-09-02,165.00\n", "target")
    ws.services.settings.heuristics["dates"] = {"slash_order": "mdy"}
    ws.services.settings.heuristics["reconciliation"] = {"derived": [{
        "name": "fund value", "target": {"path_contains": "fund_values", "key": "POLICY", "date": "VAL_DATE",
                                         "field": "FUND_VALUE"},
        "holdings": {"path_contains": "holdings", "key": "POLICY", "instrument": "FUND", "quantity": "UNITS",
                     "date": "AS_OF"},
        "prices": {"path_contains": "prices", "instrument": "FUND", "date": "PRICE_DATE", "price": "BID"}}]}
    out = ControlReconcilerAgent(ws.services, rid).execute(dictionary=[])
    summary = out.items["controls"][0]["derived"][0]
    assert summary["compared"] == 2 and summary["breaks"] == 0, (summary, [b.model_dump() for b in out.items["breaks"]])


def test_rate_tables_execute_inside_calculations():
    """COBOL subscripts and PHP array indexes become lookups into each system's own lifted table; the table's
    keys become test inputs, so a different rate and a missing rate are both proven."""
    from decimal import Decimal
    from reasoning.deterministic import canonical as canon
    from reasoning.deterministic.equivalence import Program, Step, compare, simulate
    src = canon.cobol("RATE-PCT(WS-CODE) * SUM-ASSURED / 100")
    tgt = canon.php("self::RATE_PCT[$code] * $sumAssured / 100")
    assert src == tgt == "RATE_PCT (CODE) * SUM_ASSURED / 100"
    s = Program("S", "s.cbl", "source", [Step("r1", "PREMIUM", src, [], "calculation", 0)],
                tables={"RATE_PCT": {"A01": "1.5", "B02": "1.75", "C03": "2"}})
    t = Program("T", "t.php", "target", [Step("r2", "PREMIUM", tgt, [], "calculation", 0)],
                tables={"RATE_PCT": {"A01": "1.5", "B02": "1.8"}})
    assert simulate(s, {"CODE": "B02", "SUM_ASSURED": Decimal("1000")})["PREMIUM"] == Decimal("17.5")
    c = compare("PREMIUM", s, t)
    assert c.category == "table"
    assert {d["inputs"]["CODE"] for d in c.divergent} == {"B02", "C03"}
    assert c.lookup_gaps == ["target: RATE_PCT has no entry for C03"]
