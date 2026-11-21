#!/usr/bin/env python3
"""Local read-only reviewer UI for inspecting generated Track 1 case decisions."""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from engine import DataStore, read_jsonl


INSTITUTIONS = {"bank", "emoney", "investment", "payments_fx"}
ASSETS = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}


class ReviewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, root: Path):
        super().__init__(address, handler)
        self.root = root.resolve()
        self.asset_root = Path(__file__).resolve().parent
        self.stores: dict[str, DataStore] = {}
        self.predictions: dict[str, dict[str, dict]] = {}

    def store(self, institution: str) -> DataStore:
        if institution not in INSTITUTIONS:
            raise ValueError("Unknown institution")
        if institution not in self.stores:
            self.stores[institution] = DataStore(self.root / "institutions" / institution)
        return self.stores[institution]

    def output(self, institution: str) -> dict[str, dict]:
        if institution not in INSTITUTIONS:
            raise ValueError("Unknown institution")
        if institution not in self.predictions:
            path = self.root / "output" / (institution + ".jsonl")
            rows = read_jsonl(path) if path.is_file() else []
            self.predictions[institution] = {str(row.get("case_id")): row for row in rows}
        return self.predictions[institution]


class Handler(BaseHTTPRequestHandler):
    server: ReviewerServer

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            query = parse_qs(parsed.query)
            institution = query.get("institution", ["bank"])[0]
            try:
                if parsed.path == "/api/summary":
                    self._summary(institution)
                elif parsed.path == "/api/cases":
                    self._cases(institution, query)
                elif parsed.path.startswith("/api/case/"):
                    case_id = parsed.path.rsplit("/", 1)[-1]
                    self._case(institution, case_id)
                else:
                    self._json({"error": "Not found"}, 404)
            except (ValueError, OSError, KeyError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        asset_name = ASSETS.get(parsed.path)
        if not asset_name:
            self._json({"error": "Not found"}, 404)
            return
        path = self.server.asset_root / asset_name
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "UI asset unavailable"}, 500)
            return
        content_type = "text/html; charset=utf-8" if asset_name.endswith(".html") else "text/javascript; charset=utf-8" if asset_name.endswith(".js") else "text/css; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _summary(self, institution: str):
        store = self.server.store(institution)
        output = self.server.output(institution)
        counts = {"CLOSE": 0, "RFI": 0, "ESCALATE": 0}
        for row in output.values():
            verdict = row.get("verdict")
            if verdict in counts:
                counts[verdict] += 1
        self._json({
            "institution": institution,
            "case_count": len(store.cases),
            "completed_count": len(output),
            "counts": counts,
            "has_output": bool(output),
        })

    def _cases(self, institution: str, query):
        store = self.server.store(institution)
        output = self.server.output(institution)
        search = query.get("q", [""])[0].strip().lower()
        verdict_filter = query.get("verdict", ["ALL"])[0].upper()
        rows = []
        for case in store.cases:
            customer = store.customers.get(str(case.get("customer_id")), {})
            result = output.get(str(case.get("case_id")), {})
            alerts = store.alerts_for(case)
            alert_names = [str(alert.get("rule_name", "")) for alert in alerts]
            haystack = " ".join([
                str(case.get("case_id", "")), str(customer.get("name", "")),
                str(customer.get("customer_id", "")), " ".join(alert_names),
            ]).lower()
            verdict = str(result.get("verdict", "PENDING"))
            if search and search not in haystack:
                continue
            if verdict_filter != "ALL" and verdict != verdict_filter:
                continue
            rows.append({
                "case_id": case.get("case_id"),
                "customer_id": case.get("customer_id"),
                "customer_name": customer.get("name", "Unknown customer"),
                "customer_kind": customer.get("kind", ""),
                "alert_count": len(alerts),
                "alert_names": alert_names,
                "opened_at": case.get("opened_at"),
                "held_payment": bool(case.get("held_payment_id")),
                "verdict": verdict,
                "reason_code": result.get("reason_code"),
                "reason": result.get("reason", "Run the decision system to generate a case result."),
            })
        self._json({"cases": rows, "total": len(rows), "institution": institution})

    def _case(self, institution: str, case_id: str):
        store = self.server.store(institution)
        case = next((row for row in store.cases if str(row.get("case_id")) == case_id), None)
        if case is None:
            self._json({"error": "Case not found"}, 404)
            return
        context = store.case_context(case)
        result = self.server.output(institution).get(case_id)
        docs = []
        for doc in store.evidence_by_customer.get(str(case.get("customer_id")), []):
            docs.append({
                "id": doc.meta.get("doc_id"), "type": doc.meta.get("type"),
                "title": doc.meta.get("title"), "dated": doc.meta.get("dated"),
                "text": doc.text[:7000],
            })
        self._json({
            "institution": institution,
            "case": case,
            "customer": context.get("customer", {}),
            "alerts": context.get("alerts", []),
            "transactions": context.get("transactions", []),
            "documents": docs,
            "previous_reviews": context.get("previous_reviews", []),
            "internal_list_matches": context.get("internal_list_matches", []),
            "decision": result,
        })


def main():
    parser = argparse.ArgumentParser(description="Serve the read-only Track 1 case review UI")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="hackathon folder containing institutions/ and output/")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ReviewerServer(("127.0.0.1", args.port), Handler, args.root)
    print(f"TM Casework running at http://127.0.0.1:{args.port}", file=sys.stderr)
    print(f"Data root: {server.root}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
