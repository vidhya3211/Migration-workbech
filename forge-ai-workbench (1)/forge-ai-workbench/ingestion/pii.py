"""PII obfuscation before staging.

Direct identifiers (NI number, email, phone, bank details) are replaced with keyed HMAC tokens before a data
extract reaches the evidence store: equality is preserved (matching, reconciliation still work) but the raw
value never lands. Quasi-identifiers needed for matching (name, DOB, postcode) are staged but classified
``confidential``: they stay in the controlled evidence store and are never logged, traced, compressed into
telemetry or sent to an LLM (see guardrails/pii).

Obfuscation streams row by row (``obfuscate_delimited`` / ``obfuscate_rows``) so a multi-GB extract is never
held in memory. Column names to treat as PII are configuration and a project can extend them.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import re
import warnings
from typing import Iterable, Iterator, TextIO

from config.settings import PiiSettings, secret

_DEV_KEY = b"aiwb-dev-only-pii-key"


def _key(environment: str) -> bytes:
    k = secret("AIWB_PII_HMAC_KEY")
    if k:
        return k.encode("utf-8")
    if environment == "production":
        raise RuntimeError("AIWB_PII_HMAC_KEY must be provided from Key Vault in production")
    warnings.warn("using the development PII key; set AIWB_PII_HMAC_KEY outside local development", stacklevel=2)
    return _DEV_KEY


def tokenise(value: str, environment: str, key: bytes | None = None) -> str:
    if not value:
        return value
    return "tok_" + hmac.new(key or _key(environment), value.strip().upper().encode("utf-8"),
                             hashlib.sha256).hexdigest()[:20]


def _norm(col: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", col.strip().lower()).strip("_")


def column_matches(col: str, names: list[str]) -> bool:
    c = _norm(col)
    return any(c == n or c.endswith("_" + n) or c.startswith(n + "_") for n in names)


def looks_like_delimited_extract(text: str, delimiters: list[str] | None = None) -> bool:
    first = text.split("\n", 1)[0]
    if first.lstrip().startswith(("#", "//", "--", "<", "{")):
        return False
    return any(first.count(d) >= 2 for d in (delimiters or [","]))


def looks_like_csv_extract(text: str) -> bool:   # kept for callers that only handle commas
    return looks_like_delimited_extract(text, [","])


def obfuscate_rows(header: list[str], rows: Iterable[list[str]], pii: PiiSettings,
                   environment: str) -> tuple[list[int], list[str], Iterator[list[str]]]:
    """(obfuscated column indexes, confidential column names, row iterator with identifiers tokenised)."""
    obf_idx = [i for i, c in enumerate(header) if column_matches(c, pii.obfuscate_columns)]
    conf_cols = [c for c in header if column_matches(c, pii.classification_columns)]
    key = _key(environment) if obf_idx else None

    def gen() -> Iterator[list[str]]:
        for row in rows:
            if obf_idx:
                row = list(row)
                for i in obf_idx:
                    if i < len(row):
                        row[i] = tokenise(row[i], environment, key)
            yield row
    return obf_idx, conf_cols, gen()


def obfuscate_delimited(src: TextIO, out: TextIO, pii: PiiSettings, environment: str,
                        delimiters: list[str]) -> tuple[list[str], list[str], str]:
    """Stream a delimited extract from ``src`` to ``out`` with identifiers tokenised.

    Returns (obfuscated columns, confidential columns, delimiter)."""
    head = src.readline()
    counts = {d: head.count(d) for d in delimiters}
    delim = max(counts, key=lambda d: counts[d]) if counts and max(counts.values()) > 0 else ","
    header = next(csv.reader([head], delimiter=delim), [])
    obf_idx, conf_cols, rows = obfuscate_rows(header, csv.reader(src, delimiter=delim), pii, environment)
    w = csv.writer(out, delimiter=delim, lineterminator="\n")
    w.writerow(header)
    for row in rows:
        w.writerow(row)
    return [header[i] for i in obf_idx], conf_cols, delim


def obfuscate_csv(text: str, pii: PiiSettings, environment: str) -> tuple[str, list[str], list[str]]:
    """In-memory convenience for small inputs (tests); production paths use ``obfuscate_delimited``."""
    out = io.StringIO()
    obf, conf, _ = obfuscate_delimited(io.StringIO(text), out, pii, environment, [","])
    return (out.getvalue() if obf else text), obf, conf
