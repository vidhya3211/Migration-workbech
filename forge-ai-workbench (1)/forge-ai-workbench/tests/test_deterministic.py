from decimal import Decimal

import pytest

from guardrails.deterministic.evidence import EvidenceVerifier
from guardrails.deterministic.guard import DeterministicGuard, self_test
from reasoning.deterministic import canonical as c
from reasoning.deterministic.equivalence import Program, Step, compare
from reasoning.deterministic.expressions import Unknown, evaluate
from reasoning.deterministic.transform import TransformError, apply, values_equal
from schemas.mapping import TransformationSpec


def test_guard_self_test_refuses_every_fabrication():
    results = self_test(lambda reader, arts: DeterministicGuard(EvidenceVerifier(reader, arts)))
    assert all(ok for _, ok in results), [label for label, ok in results if not ok]


def test_decimal_evaluation_is_exact_and_half_up():
    assert evaluate("ROUND((SA / 1000) * R / 12, 2) + FEE", {"SA": 150000, "R": "0.95", "FEE": "25.00"}) == Decimal("36.88")
    assert evaluate("ROUND(2.345, 2)") == Decimal("2.35")
    assert evaluate("TRUNC(2.349, 2)") == Decimal("2.34")
    assert evaluate("CASE WHEN Y < 3 THEN 0.05 WHEN Y < 5 THEN 0.03 ELSE 0 END", {"Y": 4}) == Decimal("0.03")
    assert evaluate("1 <= T <= 10", {"T": 10}) is True
    with pytest.raises(Unknown):
        evaluate("MISSING + 1", {})


def test_language_canonicalisers_agree():
    assert c.cobol("(WS-BASE-PREMIUM / 12) + WS-ADMIN-FEE", rounded_scale=2) == "ROUND((BASE_PREMIUM / 12) + ADMIN_FEE, 2)"
    assert c.php("$policy->holderAge > self::AGE_LOAD_THRESHOLD") == "HOLDER_AGE > AGE_LOAD_THRESHOLD"
    assert c.cobol("POL-HOLDER-AGE >= WS-AGE-LOAD-THRESHOLD") == "HOLDER_AGE >= AGE_LOAD_THRESHOLD"
    assert "CASE WHEN YEARS_IN_FORCE < 3 THEN 0.05" in c.php(
        "match (true) { $policy->yearsInForce < 3 => 0.05, default => 0.0, }")
    assert c.plsql("ROUND(v_fund * 0.015, 2)", {"V_FUND": "fund_value"}) == "ROUND(FUND_VALUE * 0.015, 2)"


def test_equivalence_proves_value_and_boundary_divergence():
    src = Program("S", "src", "source", [Step("a", "FEE", "25.00", [], "constant", 0),
                                         Step("b", "LIMIT", "60", [], "constant", 1),
                                         Step("c", "LOAD", "1", [], "assignment", 2),
                                         Step("d", "LOAD", "1.25", ["AGE >= LIMIT"], "assignment", 3)])
    tgt = Program("T", "tgt", "target", [Step("A", "FEE", "30.00", [], "constant", 0),
                                         Step("B", "LIMIT", "60", [], "constant", 1),
                                         Step("C", "LOAD", "1", [], "assignment", 2),
                                         Step("D", "LOAD", "1.25", ["AGE > LIMIT"], "assignment", 3)])
    fee = compare("FEE", src, tgt)
    assert fee.category == "value" and fee.max_abs_delta == Decimal("5.00")
    load = compare("LOAD", src, tgt)
    assert load.category == "condition"
    assert {d["inputs"]["AGE"] for d in load.divergent} == {"60"}
    assert compare("LIMIT", src, tgt).category == "equivalent"


def test_transformations_are_exact_and_strict():
    assert apply(TransformationSpec(kind="date_format", params={"null_sentinels": ["00000000"]}, description=""),
                 "20210315") == "2021-03-15"
    assert apply(TransformationSpec(kind="date_format", params={"null_sentinels": ["00000000"]}, description=""),
                 "00000000") is None
    with pytest.raises(TransformError):
        apply(TransformationSpec(kind="code_map", params={"map": {"Y": "true"}, "strict": True}, description=""), "Q")
    assert values_equal("36.88", "41.88", "decimal") == (False, Decimal("5.00"))
    assert values_equal("true", "Y", "boolean")[0]
