from agents.discovery.cartographer import decide_text
from tools.parsers import ddl, document, jcl, plsql, spreadsheet
from tools.parsers.php import PhpParser, tokenize


def test_cartographer_trusts_content_over_extension(samples):
    reasons = []
    text = (samples / "source/landing/ADMFEE_20240101.txt").read_text()
    assert decide_text(".txt", text, reasons)[0] == "cobol_program"
    assert decide_text(".sql", (samples / "source/ddl/CAPITA_LTA.sql").read_text(), [])[0] == "oracle_ddl"
    assert decide_text(".pkb", (samples / "source/plsql/PKG_BONUS.pkb").read_text(), [])[0] == "plsql"


def test_php_early_return_guards_later_statements(samples):
    p = PhpParser(tokenize((samples / "target/integro/SurrenderValue.php").read_text())).run()
    later = [f for f in p.findings if f.start_line == 24]
    assert later and any("NOT" in c and "'L'" in c for c in later[0].conditions)


def test_plsql_constants_conditions_and_aliases(samples):
    p = plsql.PlsqlParser(plsql.tokenize((samples / "source/plsql/PKG_BONUS.pkb").read_text())).run()
    kinds = {(f.kind, f.target) for f in p.findings}
    assert ("constant", "c_terminal_rate") in kinds and ("calculation", "terminal_bonus()") in kinds
    guarded = [f for f in p.findings if f.target == "v_rate"]
    assert any("NOT (" in " ".join(f.conditions) for f in guarded)
    assert p.aliases["V_FUND"] == "fund_value"


def test_jcl_steps_and_datasets(samples):
    jobs = jcl.parse((samples / "source/landing/NIGHTLY.jcl").read_text())
    assert [s.program for s in jobs[0].steps] == ["PREMCALC", "SURRVAL"]
    assert jobs[0].steps[0].dds[0]["dsn"] == "CAPITA.LTA.POLICY"


def test_ddl_columns_constraints_and_domains(samples):
    model = ddl.parse((samples / "source/ddl/CAPITA_LTA.sql").read_text())
    policy = next(t for t in model.tables if t.name == "POLICY")
    status = next(c for c in policy.columns if c.name == "POL_STATUS")
    assert (status.data_type, status.nullable) == ("string", False)
    check = next(c for c in policy.constraints if c.name == "CK_POL_STATUS")
    assert ddl.allowed_values(check.expression, "POL_STATUS") == ["A", "L", "P", "R"]
    assert model.objects[0].kind == "trigger"


def test_spreadsheet_formula_labels(samples):
    findings, summary = spreadsheet.extract((samples / "source/spreadsheets/LTA_Rates.xlsx").read_bytes())
    calc = next(f for f in findings if f.kind == "calculation")
    assert calc.cell == "B6" and "Monthly admin fee" in calc.expression
    assert summary["defined_names"] == ["ADMIN_FEE"]


def test_document_assertions_span_lines(samples):
    text = (samples / "source/docs/LTA_Product_Guide.md").read_text()
    found, unparsed = document.extract_assertions(document.lines_for("md", text, text.encode()), "md")
    by_name = {(a.name, a.value): a for a in found}
    assert ("exit fee", "50.00") in by_name
    rate = by_name[("penalty rate", "0.03")]
    assert (rate.span.line_start, rate.span.line_end) == (9, 10) and "3 <= YEARS_IN_FORCE < 5" == rate.condition
    assert not unparsed
