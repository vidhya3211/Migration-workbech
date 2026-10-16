"""Product x rule-domain coverage: where the certified rule base is complete, where it differs, and where a
product has no rule at all for a domain.

The product dimension is the project's choice (``heuristics.coverage.product_field``: a source data column such
as a product or plan code); without one the matrix has a single "all products" column. A rule applies to a
product when one of its conditions names the product field with that product's value; a rule with no product
condition applies to every product. Each cell states, for the rules that apply:

* ``certified`` - every one is certified by a named person;
* ``differs`` - a difference between source and target is not yet resolved;
* ``source_only`` / ``target_only`` - only one system implements the domain for this product;
* ``candidate`` - rules exist but none is certified yet;
* ``none`` - no rule of this domain applies to the product (absent, or not found).
"""
from __future__ import annotations

import re
from collections import defaultdict

from db import models as m
from reasoning.deterministic.naming import canonical_name

MAX_PRODUCTS = 60


def coverage(kc, staging, run_id: str, settings) -> dict:
    cfg = settings.heuristics.get("coverage") or {}
    field = cfg.get("product_field")
    products: dict[str, int] = {}
    if field:
        for t in staging.tables(run_id, "source"):
            col = next((i for i, c in enumerate(t.columns) if c.upper() == str(field).upper()), None)
            if col is None:
                continue
            values, _ = staging.distinct_values(t.artefact_id, col, MAX_PRODUCTS)
            for v, n in values:
                if v:
                    products[v] = products.get(v, 0) + int(n)
    labels = sorted(products) or ["(all products)"]
    canon_field = canonical_name(field) if field else None
    rules = kc.rows(m.Rule, run_id)
    diffs = kc.rows(m.Differential, run_id)
    open_diff = {d.canonical_name for d in diffs if d.status not in ("certified", "rejected")}
    domains = sorted({r.domain or "general" for r in rules if r.kind not in ("schedule", "constraint", "assertion")})
    cells: dict[tuple[str, str], dict] = defaultdict(lambda: {"rules": 0, "certified": 0, "roles": set(), "open": 0})

    def applies_to(conds: list[str]) -> list[str] | None:
        if not field:
            return None
        hits = []
        for c in conds or []:
            cu = c.upper()
            if canon_field and canon_field not in canonical_name(cu) and str(field).upper() not in cu:
                continue
            hits += [p for p in labels if re.search(rf"(?<![A-Z0-9]){re.escape(p.upper())}(?![A-Z0-9])", cu)]
        return hits or None

    for r in rules:
        if r.kind in ("schedule", "constraint", "assertion"):
            continue
        targets = applies_to(list(r.conditions or [])) or labels
        for p in targets:
            c = cells[(p, r.domain or "general")]
            c["rules"] += 1
            c["certified"] += r.status == "certified"
            c["roles"].add(r.system_role)
            c["open"] += r.canonical_name in open_diff
    matrix = []
    for d in domains:
        row = {"domain": d, "cells": {}}
        for p in labels:
            c = cells.get((p, d))
            if not c or not c["rules"]:
                state = "none"
            elif c["open"]:
                state = "differs"
            elif c["roles"] == {"source"}:
                state = "source_only"
            elif c["roles"] == {"target"}:
                state = "target_only"
            elif c["certified"] == c["rules"]:
                state = "certified"
            elif c["certified"]:
                state = "partly_certified"
            else:
                state = "candidate"
            row["cells"][p] = {"state": state, "rules": c["rules"] if c else 0, "certified": c["certified"] if c else 0}
        matrix.append(row)
    return {"run_id": run_id, "product_field": field, "products": [{"product": p, "records": products.get(p)} for p in labels],
            "domains": domains, "matrix": matrix}
