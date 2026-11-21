import unittest

from engine import (
    DECISION_CODES,
    Evidence,
    _parse_model_json,
    finalize_response,
    make_packet,
    offline_decision,
)


def context(alerts, docs=(), transactions=(), reviews=(), list_rows=(), customer=None, case_id="CASE-1"):
    alert_ids = [alert["alert_id"] for alert in alerts]
    alert_ids_for_tx = {str(tid) for alert in alerts for tid in alert.get("transaction_ids", [])}
    txids = {str(row["transaction_id"]) for row in transactions}
    docs = list(docs)
    return {
        "case": {"case_id": case_id, "institution": "bank", "customer_id": "C-1", "alert_ids": alert_ids, "held_payment_id": None,
                 "review_window": {"from": "2026-09-01", "to": "2026-09-30"}},
        "customer": customer or {"customer_id": "C-1", "kind": "individual", "name": "Alex Example", "declared": {"monthly_income_gbp": 2500}},
        "alerts": alerts,
        "transactions": list(transactions),
        "alert_transaction_ids": alert_ids_for_tx,
        "documents": docs,
        "all_document_ids": [d.meta["doc_id"] for d in docs],
        "previous_reviews": list(reviews),
        "internal_list_matches": list(list_rows),
        "selectable_evidence_ids": {d.meta["doc_id"] for d in docs} | alert_ids_for_tx | txids | {str(r["entry_id"]) for r in list_rows},
        "transaction_totals": {},
    }


def alert(alert_id="A-1", rule_id="TM01", message="Activity exceeds the declared profile.", measured=None, transaction_ids=None):
    return {"alert_id": alert_id, "case_id": "CASE-1", "rule_id": rule_id, "rule_name": "Monitoring alert",
            "message": message, "measured": measured or {}, "transaction_ids": transaction_ids or []}


def doc(doc_id, kind, text, title="Evidence record"):
    return Evidence({"doc_id": doc_id, "type": kind, "title": title, "dated": "2026-09-12"}, text)


class DecisionStressTests(unittest.TestCase):
    def test_fraud_report_overrides_benign_invoice(self):
        tx = {"transaction_id": "T-1", "amount_gbp": 1000, "direction": "in", "counterparty_name": "North Bank Customer"}
        case = context(
            [alert(transaction_ids=["T-1"])],
            [doc("INV-1", "invoice", "Invoice from North Bank Customer for £1,000.00."),
             doc("RPT-1", "sending_bank_fraud_report", "Authorised push payment scam. Please freeze funds.")],
            [tx],
        )
        decision = offline_decision(case)
        self.assertEqual(decision["verdict"], "ESCALATE")
        self.assertEqual(decision["reason_code"], "ESC_THIRD_PARTY_FUNDING")

    def test_customer_admission_of_receiving_and_forwarding_money_escalates(self):
        case = context(
            [alert("A-1", "TM02")],
            [doc("CHAT-1", "support_chat", "Clients pay into my account and I send it on to the company's crypto account.")],
        )
        decision = offline_decision(case)
        self.assertEqual(decision["verdict"], "ESCALATE")
        self.assertEqual(decision["reason_code"], "ESC_MULE_ACTIVITY")

    def test_threshold_evasion_note_with_repeated_deposits_escalates(self):
        case = context(
            [alert("A-1", "TM03")],
            [doc("NTE-1", "staff_note", "Customer asked about aggregation and whether keeping each deposit below the reporting threshold would avoid reporting.")],
        )
        decision = offline_decision(case)
        self.assertEqual(decision["verdict"], "ESCALATE")
        self.assertEqual(decision["reason_code"], "ESC_STRUCTURING")

    def test_same_day_recipient_or_crypto_context_alone_does_not_escalate(self):
        case = context([alert("A-1", "TM04", "A new recipient was added today; crypto counterparty context noted.")])
        decision = offline_decision(case)
        self.assertNotEqual(decision["verdict"], "ESCALATE")

    def test_exact_internal_list_identity_escalates(self):
        case = context(
            [alert("A-1", "TM22", measured={"list_entry": "L-1", "matched_on": ["date_of_birth", "name"], "matched_party": "Morgan Listed"})],
            list_rows=[{"entry_id": "L-1", "name": "Morgan Listed", "date_of_birth": "1970-05-01", "reason": "Unresolved investigation"}],
        )
        decision = offline_decision(case)
        self.assertEqual(decision["verdict"], "ESCALATE")
        self.assertEqual(decision["reason_code"], "ESC_LISTED_PARTY")
        self.assertIn("L-1", decision["evidence"])

    def test_name_only_internal_list_hit_without_resolving_evidence_is_not_auto_closed(self):
        case = context(
            [alert("A-1", "TM22", measured={"list_entry": "L-1", "matched_on": ["name"], "matched_party": "Morgan Listed"})],
            list_rows=[{"entry_id": "L-1", "name": "Morgan Listed", "date_of_birth": "1970-05-01"}],
        )
        decision = offline_decision(case)
        self.assertNotEqual(decision["verdict"], "CLOSE")

    def test_invoice_reconciles_amount_and_counterparty(self):
        tx = {"transaction_id": "T-1", "amount_gbp": 3450, "direction": "out", "counterparty_name": "Elmden Consulting Ltd"}
        case = context(
            [alert("A-1", "TM05", transaction_ids=["T-1"])],
            [doc("INV-1", "invoice", "Invoice. Pay to Elmden Consulting Ltd. Amount due £3,450.00.")],
            [tx],
        )
        decision = offline_decision(case)
        self.assertEqual(decision["verdict"], "CLOSE")
        self.assertIn("INV-1", decision["evidence"])
        self.assertEqual(decision["reason_code"], "INVOICE_MATCHES")

    def test_documented_close_reason_is_fluent_and_cites_its_sources(self):
        tx = {"transaction_id": "T-1", "amount_gbp": 3450, "direction": "out", "counterparty_name": "Elmden Consulting Ltd"}
        case = context(
            [alert("A-1", "TM05", transaction_ids=["T-1"])],
            [doc("INV-1", "invoice", "Invoice. Pay to Elmden Consulting Ltd. Amount due £3,450.00.", title="Elmden invoice")],
            [tx],
        )
        decision = offline_decision(case)
        self.assertEqual(decision["verdict"], "CLOSE")
        self.assertIn("Elmden invoice [INV-1]", decision["reason"])
        self.assertIn("transaction T-1", decision["reason"])
        self.assertIn("reconcile all alerted activity", decision["reason"])
        self.assertNotIn("support the alerted activity", decision["reason"])

    def test_invoice_with_wrong_counterparty_does_not_close(self):
        tx = {"transaction_id": "T-1", "amount_gbp": 3450, "direction": "out", "counterparty_name": "Other Supplier Ltd"}
        case = context(
            [alert("A-1", "TM05", transaction_ids=["T-1"])],
            [doc("INV-1", "invoice", "Invoice. Pay to Elmden Consulting Ltd. Amount due £3,450.00.")],
            [tx],
        )
        decision = offline_decision(case)
        self.assertNotEqual(decision["verdict"], "CLOSE")

    def test_invoice_covering_only_one_of_multiple_alerted_payments_does_not_close(self):
        tx1 = {"transaction_id": "T-1", "amount_gbp": 3450, "direction": "out", "counterparty_name": "Elmden Consulting Ltd"}
        tx2 = {"transaction_id": "T-2", "amount_gbp": 9200, "direction": "out", "counterparty_name": "Elmden Consulting Ltd"}
        case = context(
            [alert("A-1", "TM04", transaction_ids=["T-1", "T-2"])],
            [doc("INV-1", "invoice", "Invoice. Pay to Elmden Consulting Ltd. Amount due £3,450.00.")],
            [tx1, tx2],
        )
        decision = offline_decision(case)
        self.assertNotEqual(decision["verdict"], "CLOSE")

    def test_alert_threshold_breach_alone_is_not_automatic_suspicion(self):
        case = context([alert("A-1", "TM03", "Three cash deposits fired the repeated-deposit rule.")])
        decision = offline_decision(case)
        self.assertNotEqual(decision["verdict"], "ESCALATE")

    def test_malformed_early_redemption_metrics_do_not_crash_or_create_suspicion(self):
        case = context([alert("A-1", "TM10", measured={"held_days": None, "redeemed_gbp": "unknown"})])
        decision = offline_decision(case)
        self.assertEqual(decision["verdict"], "RFI")

    def test_one_explained_alert_does_not_clear_a_second_alert(self):
        tx = {"transaction_id": "T-1", "amount_gbp": 3450, "direction": "out", "counterparty_name": "Elmden Consulting Ltd"}
        case = context(
            [alert("A-1", "TM05", transaction_ids=["T-1"]), alert("A-2", "TM06", "Higher-risk corridor payment.")],
            [doc("INV-1", "invoice", "Invoice. Pay to Elmden Consulting Ltd. Amount due £3,450.00.")],
            [tx],
        )
        decision = offline_decision(case)
        self.assertNotEqual(decision["verdict"], "CLOSE")
        self.assertEqual(decision["alert_ids"], ["A-1", "A-2"])

    def test_old_review_alone_never_explains_current_alert(self):
        case = context(
            [alert("A-1", "TM01")],
            reviews=[{"review_id": "REV-OLD", "reviewed_on": "2025-01-01", "rule_id": "TM01", "verdict": "CLOSE", "note": "Earlier bonus was documented."}],
        )
        decision = offline_decision(case)
        self.assertNotEqual(decision["verdict"], "CLOSE")

    def test_multi_alert_output_carries_every_alert_id_and_valid_source_ids(self):
        case = context([alert("A-1"), alert("A-2", "TM02")], [doc("DOC-1", "invoice", "Invoice for £40.00")])
        result = finalize_response({"verdict": "CLOSE", "reason_code": "INVOICE_MATCHES", "reason": "Supported. Based on the invoice.", "evidence": ["MADE-UP"]}, case)
        self.assertEqual(result["alert_ids"], ["A-1", "A-2"])
        self.assertTrue(set(result["evidence"]).issubset(case["selectable_evidence_ids"]))
        self.assertEqual(result["case_id"], "CASE-1")

    def test_invalid_reason_code_is_repaired_for_selected_verdict(self):
        case = context([alert()])
        result = finalize_response({"verdict": "ESCALATE", "reason_code": "INVOICE_MATCHES", "reason": "Suspicion remains. Refer this case to the MLRO."}, case)
        self.assertEqual(result["reason_code"], "ESC_UNEXPLAINED_FUNDS")
        self.assertIn(result["reason_code"], DECISION_CODES[result["verdict"]])

    def test_model_json_parser_accepts_fenced_json_but_rejects_non_object(self):
        self.assertEqual(_parse_model_json('```json\n{"verdict":"RFI"}\n```'), {"verdict": "RFI"})
        with self.assertRaises(ValueError):
            _parse_model_json("No JSON here")

    def test_case_packet_keeps_all_alerts_and_held_payment_reference(self):
        held = {"transaction_id": "T-HELD", "amount_gbp": 12000, "direction": "out", "counterparty_name": "Supplier"}
        case = context([alert("A-1", "TM08", transaction_ids=["T-HELD"])], transactions=[held])
        case["case"]["held_payment_id"] = "T-HELD"
        packet = make_packet(case)
        self.assertEqual(len(packet["alerts"]), 1)
        self.assertEqual(packet["transactions_in_review_window"][0]["transaction_id"], "T-HELD")
        self.assertTrue(packet["case"]["held_payment_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
