"""Writes the reviewed golden datasets for the sample estate (extraction, differentials, mappings, data).

Golden expectations are *reviewed* facts about the sample estate (each checked against the source line),
not a snapshot of whatever the agents currently output. Re-run after deliberately changing the samples.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

EXTRACTION = """cobol/POLREC.cpy|condition_name|ACTIVE|7
cobol/POLREC.cpy|condition_name|LAPSED|8
cobol/POLREC.cpy|condition_name|PAID_UP|9
cobol/POLREC.cpy|condition_name|SMOKER|11
cobol/POLREC.cpy|condition_name|PAY_ANNUAL|13
cobol/POLREC.cpy|condition_name|PAY_MONTHLY|14
cobol/PREMCALC.cbl|constant|ADMIN_FEE|13
cobol/PREMCALC.cbl|constant|SMOKER_LOADING|14
cobol/PREMCALC.cbl|constant|AGE_LOAD_THRESHOLD|15
cobol/PREMCALC.cbl|constant|AGE_LOAD_FACTOR|16
cobol/PREMCALC.cbl|constant|ANNUAL_DISCOUNT_RATE|17
cobol/PREMCALC.cbl|assignment|RISK_FACTOR|33
cobol/PREMCALC.cbl|calculation|RISK_FACTOR|41
cobol/PREMCALC.cbl|calculation|RISK_FACTOR|44
cobol/PREMCALC.cbl|assignment|TERM_FACTOR|50
cobol/PREMCALC.cbl|assignment|TERM_FACTOR|52
cobol/PREMCALC.cbl|assignment|TERM_FACTOR|54
cobol/PREMCALC.cbl|calculation|BASE_PREMIUM|56
cobol/PREMCALC.cbl|calculation|MONTHLY_PREMIUM|59
cobol/PREMCALC.cbl|calculation|ANNUAL_PREMIUM|61
cobol/PREMCALC.cbl|calculation|DISCOUNT|65
cobol/PREMCALC.cbl|calculation|ANNUAL_PREMIUM|67
cobol/PREMCALC.cbl|assignment|DISCOUNT|69
cobol/SURRVAL.cbl|constant|MINIMUM_SURRENDER|12
cobol/SURRVAL.cbl|constant|EXIT_FEE|13
cobol/SURRVAL.cbl|assignment|SURRENDER_VALUE|17
cobol/SURRVAL.cbl|assignment|PENALTY_RATE|26
cobol/SURRVAL.cbl|assignment|PENALTY_RATE|28
cobol/SURRVAL.cbl|assignment|PENALTY_RATE|30
cobol/SURRVAL.cbl|calculation|PENALTY_AMOUNT|33
cobol/SURRVAL.cbl|calculation|SURRENDER_VALUE|35
cobol/SURRVAL.cbl|assignment|SURRENDER_VALUE|39
docs/LTA_Product_Guide.md|assertion|MONTHLY_ADMINISTRATION_CHARGE|4
docs/LTA_Product_Guide.md|assertion|AGE_LOAD_FACTOR|5
docs/LTA_Product_Guide.md|assertion|AGE_LOADING_THRESHOLD|5
docs/LTA_Product_Guide.md|assertion|SMOKER_LOADING|6
docs/LTA_Product_Guide.md|assertion|PENALTY_RATE|9
docs/LTA_Product_Guide.md|assertion|EXIT_FEE|10
integro/PremiumCalculator.php|constant|ADMIN_FEE|11
integro/PremiumCalculator.php|constant|SMOKER_LOADING|12
integro/PremiumCalculator.php|constant|AGE_LOAD_THRESHOLD|13
integro/PremiumCalculator.php|constant|AGE_LOAD_FACTOR|14
integro/PremiumCalculator.php|constant|ANNUAL_DISCOUNT_RATE|15
integro/PremiumCalculator.php|constant|ROUNDING_PRECISION|17
integro/PremiumCalculator.php|assignment|RISK_FACTOR|21
integro/PremiumCalculator.php|calculation|RISK_FACTOR|23
integro/PremiumCalculator.php|calculation|RISK_FACTOR|27
integro/PremiumCalculator.php|assignment|TERM_FACTOR|35
integro/PremiumCalculator.php|assignment|TERM_FACTOR|37
integro/PremiumCalculator.php|assignment|TERM_FACTOR|39
integro/PremiumCalculator.php|calculation|BASE_PREMIUM|46
integro/PremiumCalculator.php|calculation|MONTHLY_PREMIUM|48
integro/PremiumCalculator.php|calculation|ANNUAL_PREMIUM|54
integro/PremiumCalculator.php|calculation|DISCOUNT|57
integro/PremiumCalculator.php|calculation|ANNUAL_PREMIUM|58
integro/PremiumCalculator.php|assignment|DISCOUNT|62
integro/SurrenderValue.php|constant|MINIMUM_SURRENDER|5
integro/SurrenderValue.php|calculation|PENALTY_RATE|11
integro/SurrenderValue.php|assignment|SURRENDER_VALUE|21
integro/SurrenderValue.php|calculation|PENALTY_AMOUNT|23
integro/SurrenderValue.php|calculation|SURRENDER_VALUE|24
integro/SurrenderValue.php|assignment|SURRENDER_VALUE|26
landing/ADMFEE_20240101.txt|assignment|ADMIN_FEE|12
landing/ADMFEE_20240101.txt|assignment|ADMIN_FEE|14
landing/NIGHTLY.jcl|schedule|LTANIGHT_STEP010|2
landing/NIGHTLY.jcl|schedule|LTANIGHT_STEP020|4
plsql/PKG_BONUS.pkb|constant|TERMINAL_RATE|2
plsql/PKG_BONUS.pkb|constant|LOYALTY_YEARS|3
plsql/PKG_BONUS.pkb|calculation|TERMINAL_BONUS_RATE|12
plsql/PKG_BONUS.pkb|assignment|TERMINAL_BONUS_RATE|14
plsql/PKG_BONUS.pkb|calculation|TERMINAL_BONUS|16
spreadsheets/LTA_Rates.xlsx|constant|MONTHLY_ADMIN_FEE|B4
spreadsheets/LTA_Rates.xlsx|calculation|MONTHLY_PREMIUM|B6"""

MAPPINGS = [
    ("POLICY", "POL_NUMBER", "lta_policy", "policy_ref", "direct"),
    ("POLICY", "POL_STATUS", "lta_policy", "status_code", "direct"),
    ("POLICY", "SMOKER_FLAG", "lta_policy", "is_smoker", "code_map"),
    ("POLICY", "PAY_FREQ", "lta_policy", "payment_frequency", "direct"),
    ("POLICY", "HOLDER_AGE", "lta_policy", "holder_age", "direct"),
    ("POLICY", "TERM_YEARS", "lta_policy", "term_years", "direct"),
    ("POLICY", "SUM_ASSURED", "lta_policy", "sum_assured", "direct"),
    ("POLICY", "BASE_RATE", "lta_policy", "base_rate", "direct"),
    ("POLICY", "FUND_VALUE", "lta_policy", "fund_value", "direct"),
    ("POLICY", "YEARS_IN_FORCE", "lta_policy", "years_in_force", "direct"),
    ("POLICY", "MONTHLY_PREMIUM", "lta_policy", "monthly_premium", "direct"),
    ("POLICY", "CUST_ID", "lta_policy", "party_id", "direct"),
    ("POLICY", "START_DT", "lta_policy", "commencement_date", "date_format"),
    ("CUSTOMER", "CUST_ID", "party", "party_id", "direct"),
    ("CUSTOMER", "CUST_NAME", "party", "full_name", "direct"),
    ("CUSTOMER", "DOB", "party", "party_birth_dt", "date_format"),
    ("CUSTOMER", "POSTCODE", "party", "post_code", "direct"),
    ("CUSTOMER", "NI_NUMBER", "party", "ni_number", "direct"),
]


def main() -> None:
    expected = [dict(zip(["file", "kind", "canonical_name", "line"], r.split("|"))) for r in EXTRACTION.splitlines()]
    (HERE / "extraction_golden.json").write_text(json.dumps({
        "description": "Rules a reviewer confirmed must be extracted, with exact kind and locator. Precision is "
                       "scored only within 'scope'.",
        "scope": sorted({e["file"] for e in expected} - {"spreadsheets/LTA_Rates.xlsx"}),
        "expected": expected}, indent=1), encoding="utf-8")
    (HERE / "differential_golden.json").write_text(json.dumps({
        "description": "Reviewed disagreements between the source and target logic in the sample estate.",
        "expected": [
            {"canonical_name": "ADMIN_FEE", "type": "value_mismatch", "critical": True, "exposure": "5"},
            {"canonical_name": "RISK_FACTOR", "type": "condition_mismatch", "critical": True,
             "divergent_input": {"HOLDER_AGE": "60"}},
            {"canonical_name": "SURRENDER_VALUE", "type": "expression_mismatch", "critical": True, "exposure": "50"},
            {"canonical_name": "TERMINAL_BONUS", "type": "missing_in_target", "critical": True},
            {"canonical_name": "TERM_FACTOR", "type": "condition_mismatch", "critical": False},
            {"canonical_name": "BASE_PREMIUM", "type": "expression_mismatch", "critical": False},
            {"canonical_name": "MONTHLY_PREMIUM", "type": "document_conflict", "critical": False}],
        "must_not_flag": ["SMOKER_LOADING", "AGE_LOAD_THRESHOLD", "AGE_LOAD_FACTOR", "MINIMUM_SURRENDER",
                          "PENALTY_AMOUNT", "ANNUAL_PREMIUM", "ANNUAL_DISCOUNT_RATE", "PENALTY_RATE", "DISCOUNT",
                          "ROUNDING_PRECISION"]}, indent=1), encoding="utf-8")
    (HERE / "mapping_golden.json").write_text(json.dumps({
        "description": "Reviewed attribute mappings. POLICY.LAST_PREM_DT has no target and must stay unmapped.",
        "expected": [dict(zip(["source_entity", "source_field", "target_entity", "target_field", "transformation"], p))
                     for p in MAPPINGS],
        "unmapped": ["POLICY.LAST_PREM_DT"]}, indent=1), encoding="utf-8")
    fee = (1, 2, 3, 4, 5, 6, 8, 9, 10, 11)
    (HERE / "data_golden.json").write_text(json.dumps({
        "description": "Reviewed data-quality and reconciliation outcomes for the sample extracts.",
        "cohorts": [
            {"entity": "POLICY", "column": "POL_STATUS", "defect_code": "invalid_code", "records": ["P000000009"],
             "root_cause": "RC-CODE-DRIFT"},
            {"entity": "POLICY", "column": "START_DT", "defect_code": "sentinel_value", "records": ["P000000009"],
             "root_cause": "RC-LEGACY-DEFAULT"}],
        "entity_matches": [["C000000010", "C000000012"]],
        "breaks": [{"record_key": f"P0000000{n:02d}", "field": "monthly_premium", "delta": "5.00"} for n in fee]
        + [{"record_key": "P000000007", "field": "monthly_premium", "delta": "-1.46"},
           {"record_key": "P000000009", "field": "commencement_date", "delta": None},
           {"record_key": "P000000012", "field": "policy_ref", "type": "missing_in_target"},
           {"record_key": "C000000012", "field": "party_id", "type": "missing_in_target"}],
        "cluster_causes": {"lta_policy.monthly_premium:value_mismatch:5.00": ["ADMIN_FEE"],
                           "lta_policy.monthly_premium:value_mismatch:-1.46": ["ADMIN_FEE", "RISK_FACTOR"]},
        "conduct_overcharge_records": [f"P0000000{n:02d}" for n in fee]}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
