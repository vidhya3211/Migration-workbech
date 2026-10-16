"""Change-data-capture (CDC) delta files applied to a staged base extract, in sequence order.

Some sources ship a full extract plus a file of changes made since (inserts, updates, deletes with a log
sequence number). The book that must be reconciled is base + changes. Each project declares its change files
(``heuristics.change_data``)::

    - changes: {path_contains: member_cdc}        # or {entity: ...}
      base: {path_contains: member_base}
      key_field: MEMBER_ID
      op_field: OP                                 # values per ``ops`` (default I / U / D)
      sequence_field: LSN                          # applied in ascending numeric order
      old_prefix: OLD_                             # optional: a non-empty OLD_<column> must equal the current value

Rules, applied strictly in sequence order:

* an **insert** of a key that already exists, an **update** or **delete** of a key that does not, a **sequence**
  that does not increase, and an **old value** that does not match the current one are *conflicts*: recorded
  with the change's line and reason, and not applied (never silently "fixed");
* an update changes only the columns the change file carries (and the base has); a delete removes the record.

Only the base records the changes touch are read (one pass), and every write lands in one transaction; memory
is proportional to the change file, not to the book.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from sqlalchemy import text

from staging.extracts import ExtractStaging, StagedExtract

KEEP_CONFLICTS = 200


def _seq(v: str | None) -> Decimal | None:
    try:
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None


def apply_changes(staging: ExtractStaging, base: StagedExtract, changes: StagedExtract, spec: dict) -> dict:
    ops = {"insert": "I", "update": "U", "delete": "D", **(spec.get("ops") or {})}
    by_op = {str(v).upper(): k for k, v in ops.items()}
    bcols = {c.upper(): i for i, c in enumerate(base.columns)}
    ccols = {c.upper(): i for i, c in enumerate(changes.columns)}
    key_f, op_f, seq_f = (str(spec[k]).upper() for k in ("key_field", "op_field", "sequence_field"))
    old_prefix = str(spec.get("old_prefix") or "").upper()
    for f, cols, where in ((key_f, bcols, "base"), (key_f, ccols, "changes"), (op_f, ccols, "changes"),
                           (seq_f, ccols, "changes")):
        if f not in cols:
            raise ValueError(f"change-data spec: {f} not in the {where} extract")
    carried = [c for c in ccols if c in bcols and c != key_f]      # columns a change can set

    def cv(row: list[str], col: str) -> str:
        i = ccols[col]
        return (row[i] if i < len(row) else "").strip()

    # 1. the changes, in sequence order (a non-numeric sequence is itself a conflict)
    ordered, conflicts, counts = [], [], {"insert": 0, "update": 0, "delete": 0, "conflicts": 0}

    def conflict(line: int, key: str, reason: str) -> None:
        counts["conflicts"] += 1
        if len(conflicts) < KEEP_CONFLICTS:
            conflicts.append({"change_line": line, "key": key, "reason": reason})
    for line, row in staging.iter_rows(changes.artefact_id):
        s = _seq(cv(row, seq_f))
        if s is None:
            conflict(line, cv(row, key_f), f"sequence {cv(row, seq_f)!r} is not a number")
            continue
        ordered.append((s, line, row))
    ordered.sort(key=lambda x: (x[0], x[1]))
    keys = sorted({cv(r, key_f) for _, _, r in ordered})

    # 2. the base records those changes touch (one pass over the base)
    current: dict[str, tuple[int, list[str]]] = {}
    with staging.engine.connect() as conn:
        for line_no, vals in conn.execute(text(
                "SELECT line_no, vals FROM extract_row WHERE artefact_id = :a AND btrim(vals ->> :k) = ANY(:keys)"),
                {"a": base.artefact_id, "k": bcols[key_f], "keys": keys}):
            current[str(vals[bcols[key_f]]).strip()] = (int(line_no), list(vals))
        next_line = int(conn.execute(text("SELECT coalesce(max(line_no), 1) FROM extract_row WHERE artefact_id = :a"),
                                     {"a": base.artefact_id}).scalar() or 1)

    # 3. apply in order
    touched: dict[str, tuple[int, list[str] | None, bool]] = {}     # key -> (line, vals or None=deleted, is_new)
    last_seq: dict[str, Decimal] = {}
    for s, line, row in ordered:
        key, op = cv(row, key_f), by_op.get(cv(row, op_f).upper())
        if op is None:
            conflict(line, key, f"unknown operation {cv(row, op_f)!r}")
            continue
        if key in last_seq and s <= last_seq[key]:
            conflict(line, key, f"sequence {s} does not follow {last_seq[key]} for this record")
            continue
        state = touched.get(key) or ((current[key][0], current[key][1], False) if key in current else None)
        exists = state is not None and state[1] is not None
        if op == "insert":
            if exists:
                conflict(line, key, "insert of a record that already exists")
                continue
            next_line += 1
            vals = [""] * len(base.columns)
            vals[bcols[key_f]] = key
            for c in carried:
                vals[bcols[c]] = cv(row, c)
            touched[key] = (next_line, vals, True)
        else:
            if not exists:
                conflict(line, key, f"{op} of a record that does not exist")
                continue
            line_no, vals, is_new = state
            stale = [c for c in carried if old_prefix and f"{old_prefix}{c}" in ccols
                     and cv(row, f"{old_prefix}{c}") != ""          # an empty old value asserts nothing
                     and cv(row, f"{old_prefix}{c}") != (vals[bcols[c]] or "").strip()]
            if stale:
                conflict(line, key, f"old value differs from the current record for {', '.join(stale)}")
                continue
            if op == "delete":
                touched[key] = (line_no, None, is_new)
            else:
                vals = list(vals)
                for c in carried:
                    vals[bcols[c]] = cv(row, c)
                touched[key] = (line_no, vals, is_new)
        counts[op] += 1
        last_seq[key] = s

    # 4. write back in one transaction
    with staging.engine.begin() as conn:
        for key, (line_no, vals, is_new) in touched.items():
            if vals is None:
                if not is_new:
                    conn.execute(text("DELETE FROM extract_row WHERE artefact_id = :a AND line_no = :l"),
                                 {"a": base.artefact_id, "l": line_no})
            elif is_new:
                conn.execute(text("INSERT INTO extract_row (artefact_id, line_no, vals) VALUES (:a, :l, CAST(:v AS jsonb))"),
                             {"a": base.artefact_id, "l": line_no, "v": json.dumps(vals, ensure_ascii=False)})
            else:
                conn.execute(text("UPDATE extract_row SET vals = CAST(:v AS jsonb) WHERE artefact_id = :a AND line_no = :l"),
                             {"a": base.artefact_id, "l": line_no, "v": json.dumps(vals, ensure_ascii=False)})
        n = conn.execute(text("SELECT count(*) FROM extract_row WHERE artefact_id = :a"), {"a": base.artefact_id}).scalar()
        conn.execute(text("UPDATE extract_table SET row_count = :n, controls = coalesce(controls, '{}'::jsonb) || "
                          "CAST(:c AS jsonb) WHERE artefact_id = :a"),
                     {"n": int(n or 0), "a": base.artefact_id,
                      "c": json.dumps({"change_data": {"changes": changes.path, **counts}})})
    staging._tables.pop(base.artefact_id, None)
    return {"base": base.path, "changes": changes.path, **counts, "conflict_details": conflicts,
            "rows_after": int(n or 0)}
