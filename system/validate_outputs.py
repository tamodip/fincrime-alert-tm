#!/usr/bin/env python3
"""Validate Track 1 development predictions against the case/output contract."""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

from engine import DECISION_CODES, DataStore, read_jsonl


def sentence_count(text: str) -> int:
    return len([piece for piece in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9£])", text.strip()) if piece.strip()])


def validate_institution(root: Path, institution: str) -> list[str]:
    store = DataStore(root / "institutions" / institution)
    output_path = root / "output" / (institution + ".jsonl")
    errors: list[str] = []
    if not output_path.is_file():
        return [f"{institution}: missing output file {output_path}"]
    rows = read_jsonl(output_path)
    by_case: dict[str, dict] = {}
    for index, row in enumerate(rows, 1):
        case_id = str(row.get("case_id", ""))
        if not case_id:
            errors.append(f"{institution}:{index}: missing case_id")
            continue
        if case_id in by_case:
            errors.append(f"{institution}:{index}: duplicate case {case_id}")
        by_case[case_id] = row
    cases = {str(case.get("case_id")): case for case in store.cases}
    for missing in sorted(set(cases) - set(by_case)):
        errors.append(f"{institution}: missing output for {missing}")
    for extra in sorted(set(by_case) - set(cases)):
        errors.append(f"{institution}: output contains unknown case {extra}")

    for case_id, case in cases.items():
        row = by_case.get(case_id)
        if not row:
            continue
        alerts = store.alerts_for(case)
        expected_alert_ids = [str(alert.get("alert_id")) for alert in alerts]
        if row.get("alert_ids") != expected_alert_ids:
            errors.append(f"{institution}:{case_id}: alert_ids do not exactly match the case")
        verdict = row.get("verdict")
        code = row.get("reason_code")
        if verdict not in DECISION_CODES:
            errors.append(f"{institution}:{case_id}: invalid verdict {verdict!r}")
        elif code not in DECISION_CODES[verdict]:
            errors.append(f"{institution}:{case_id}: reason_code {code!r} is invalid for {verdict}")
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{institution}:{case_id}: missing reason")
        else:
            count = sentence_count(reason)
            if not 2 <= count <= 4:
                errors.append(f"{institution}:{case_id}: reason has {count} sentences, expected 2–4")
            if verdict == "RFI" and not re.search(r"\b(please|provide|ask|request)\b", reason, re.I):
                errors.append(f"{institution}:{case_id}: RFI reason does not state a request")

        evidence = row.get("evidence")
        if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
            errors.append(f"{institution}:{case_id}: evidence must be a list of IDs")
            continue
        if len(set(evidence)) != len(evidence):
            errors.append(f"{institution}:{case_id}: evidence contains duplicate IDs")
        customer_id = str(case.get("customer_id", ""))
        valid_ids = {str(doc.meta.get("doc_id")) for doc in store.evidence_by_customer.get(customer_id, [])}
        alert_tx_ids = {str(tid) for alert in alerts for tid in alert.get("transaction_ids", [])}
        if case.get("held_payment_id"):
            alert_tx_ids.add(str(case["held_payment_id"]))
        review = case.get("review_window") or {}
        start, end = str(review.get("from", "")), str(review.get("to", ""))
        for tx in store.transactions_by_customer.get(customer_id, []):
            day = str(tx.get("booked_at", ""))[:10]
            if (not start or day >= start) and (not end or day <= end):
                alert_tx_ids.add(str(tx.get("transaction_id", "")))
        list_ids = {
            str(alert.get("measured", {}).get("list_entry"))
            for alert in alerts
            if alert.get("measured", {}).get("list_entry")
        }
        valid_ids.update(alert_tx_ids)
        valid_ids.update(list_ids)
        invalid_ids = set(evidence) - valid_ids
        if invalid_ids:
            errors.append(f"{institution}:{case_id}: unknown or out-of-case evidence IDs {sorted(invalid_ids)[:4]}")
    return errors


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    errors: list[str] = []
    for institution in ("bank", "emoney", "investment", "payments_fx"):
        errors.extend(validate_institution(root, institution))
    if errors:
        for error in errors[:100]:
            print("ERROR", error)
        if len(errors) > 100:
            print(f"... and {len(errors) - 100} more")
        return 1
    print("Validated 4,000 output rows across all four institutions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
