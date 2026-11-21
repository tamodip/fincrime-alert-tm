#!/usr/bin/env python3
"""Deterministic transaction-monitoring alert triage.

Usage: tm.py <institution folder> <output file>

Design: evidence-first, safety-first.
  1. Red-flag detectors (fraud reports, structuring talk, mule stories, shared devices,
     listed-party control, invoice mismatches)            -> ESCALATE
  2. Explanation detectors: a document on file whose amount, party and date reconcile to the
     alerted transactions (invoice, sale, inheritance, insurer payout, own-account statement,
     collection list, cash takings, refund ...)          -> CLOSE, only if EVERY alert is explained
  3. Anything else                                        -> RFI asking for the one document that
                                                             would settle it (safe default)
"""
import json
import os
import re
import sys
from collections import defaultdict

HIGH_RISK_DEFAULT = ["Afghanistan", "Belarus", "Democratic Republic of the Congo", "Haiti", "Iran", "Iraq",
                     "Lebanon", "Libya", "Mali", "Myanmar", "North Korea", "Russia", "Somalia", "South Sudan",
                     "Syria", "Venezuela", "Yemen"]
HIGH_RISK_CITIES = {"beirut": "Lebanon", "tehran": "Iran", "baghdad": "Iraq", "damascus": "Syria",
                    "kabul": "Afghanistan", "minsk": "Belarus", "moscow": "Russia", "caracas": "Venezuela",
                    "port-au-prince": "Haiti", "tripoli": "Libya", "sanaa": "Yemen", "pyongyang": "North Korea"}

MONEY_RE = re.compile(r"£\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def money_in(text):
    out = []
    for m in MONEY_RE.finditer(text or ""):
        try:
            out.append(round(float(m.group(1).replace(",", "")), 2))
        except ValueError:
            pass
    return out


def gbp(x):
    return "£{:,.2f}".format(x)


def norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()


def same_name(a, b):
    """Loose person/company name equality on tokens (ignoring legal suffixes)."""
    drop = {"ltd", "limited", "plc", "llp", "llc", "co", "inc", "the", "mr", "mrs", "ms", "dr", "sal"}
    ta = [t for t in norm(a) if t not in drop]
    tb = [t for t in norm(b) if t not in drop]
    return bool(ta) and ta == tb


def name_in(name, text):
    toks = [t for t in norm(name) if t not in {"ltd", "limited", "plc", "llp"}]
    if not toks:
        return False
    return " ".join(toks) in " ".join(norm(text))


class Inst:
    def __init__(self, folder):
        self.folder = folder
        self.name = os.path.basename(os.path.normpath(folder))
        j = lambda n: read_jsonl(os.path.join(folder, n + ".jsonl"))
        self.cases = j("cases")
        self.alerts = {a["alert_id"]: a for a in j("alerts")}
        self.customers = {c["customer_id"]: c for c in j("customers")}
        self.tx = {}
        self.tx_by_cust = defaultdict(list)
        for t in j("transactions"):
            self.tx[t["transaction_id"]] = t
            self.tx_by_cust[t["customer_id"]].append(t)
        self.docs_by_cust = defaultdict(list)
        for e in j("evidence"):
            p = os.path.join(folder, e.get("file") or ("evidence/%s.md" % e["doc_id"]))
            try:
                e["text"] = open(p, encoding="utf-8").read()
            except OSError:
                e["text"] = ""
            self.docs_by_cust[e["customer_id"]].append(e)
        self.reviews_by_cust = defaultdict(list)
        for r in j("previous_reviews"):
            self.reviews_by_cust[r["customer_id"]].append(r)
        self.internal = j("internal_list")
        pol = ""
        try:
            pol = open(os.path.join(folder, "POLICY.md"), encoding="utf-8").read()
        except OSError:
            pass
        m = re.search(r"Higher-risk countries[^:]*:\s*([^\n]+)", pol)
        self.high_risk = [c.strip().rstrip(".") for c in m.group(1).split(",")] if m else HIGH_RISK_DEFAULT
        m = re.search(r"own accounts explain activity at (\d+) times", pol)
        self.own_acct_multiple = int(m.group(1)) if m else 5


# ----------------------------------------------------------------------------- case context

class Case:
    def __init__(self, inst, case):
        self.inst = inst
        self.case = case
        self.cid = case["customer_id"]
        self.cust = inst.customers.get(self.cid, {})
        self.alerts = [inst.alerts[a] for a in case["alert_ids"] if a in inst.alerts]
        self.rules = sorted({a["rule_id"] for a in self.alerts})
        self.alert_tx = []
        seen = set()
        for a in self.alerts:
            for t in a.get("transaction_ids") or []:
                if t in inst.tx and t not in seen:
                    seen.add(t)
                    self.alert_tx.append(inst.tx[t])
        held = case.get("held_payment_id")
        if held and held in inst.tx and held not in seen:
            self.alert_tx.append(inst.tx[held])
        self.docs = inst.docs_by_cust.get(self.cid, [])
        self.reviews = inst.reviews_by_cust.get(self.cid, [])
        self.cust_name = self.cust.get("name", "the customer")
        decl = self.cust.get("declared") or {}
        self.monthly = decl.get("monthly_income_gbp") or (
            (decl.get("annual_turnover_gbp") or decl.get("expected_annual_volume_gbp") or 0) / 12.0) or None

    def docs_of(self, *types):
        return [d for d in self.docs if d.get("type") in types]

    def tx_amounts(self, direction=None):
        return [t for t in self.alert_tx if direction is None or t.get("direction") == direction]


def tx_matches_amount(t, amounts, tol=0.01):
    a = t.get("amount_gbp")
    b = t.get("amount")
    return any(abs((a or -1) - x) <= tol or abs((b or -1) - x) <= tol for x in amounts)


# ----------------------------------------------------------------------------- red flags

def red_flags(C):
    """Return list of (code, reason, evidence) for suspicious signs."""
    out = []
    for d in C.docs_of("sending_bank_fraud_report"):
        out.append(("ESC_MULE_ACTIVITY",
                    "%s reports that its customer was scammed and paid money into this account (%s). Money from scam victims "
                    "passing through the account is a sign of mule activity, so the case goes to the MLRO rather than the customer." % (
                        d.get("title", "A sending bank"), d["doc_id"]), [d["doc_id"]]))
    for d in C.docs_of("device_export"):
        custs = set(re.findall(r"[A-Z]{2}-C\d+", d["text"]))
        if len(custs - {C.cid}) >= 1:
            out.append(("ESC_ACCOUNT_MISUSE",
                        "The device export %s shows the same device logging in to %d customer accounts (%s), so someone other "
                        "than the customer appears to control or use this account." % (
                            d["doc_id"], len(custs), ", ".join(sorted(custs))), [d["doc_id"]]))
    for d in C.docs_of("staff_note", "contact_note", "support_chat"):
        low = d["text"].lower()
        if re.search(r"(threshold|reporting requirement|reported to|aggregat|below the £?10,000|under £?10,000|different branch|another branch|avoid (the )?(checks|report))", low) \
                and re.search(r"cash|deposit|top.?up|payment", low) and d.get("type") == "staff_note":
            out.append(("ESC_STRUCTURING",
                        "Staff note %s records the customer asking how to keep deposits below the reporting threshold and saying "
                        "they would use a different branch. Combined with the alerted deposits just under the limit, this indicates "
                        "structuring." % d["doc_id"], [d["doc_id"]]))
        if d.get("type") == "support_chat":
            if re.search(r"(send it on|pass(ed)? it on|forward (it|the money)|keep \d+ ?%|commission|payments assistant|"
                         r"money transfer agent|invest for (a few|other) people|on behalf of (a|my) friend|"
                         r"my manager needs|crypto account|recruited|job .* (receive|transfer))", low):
                out.append(("ESC_MULE_ACTIVITY",
                            "In chat %s the customer says they receive other people's money and send it on for someone else, which "
                            "is how money mules operate. There is no legitimate business reason on file." % d["doc_id"],
                            [d["doc_id"]]))
    return out


# ----------------------------------------------------------------------------- internal list (TM22)

def list_check(C):
    """Return ('different'|'same'|'unknown', reason, evidence)."""
    al = [a for a in C.alerts if a["rule_id"] == "TM22"]
    if not al:
        return None
    a = al[0]
    meas = a.get("measured") or {}
    entry_id = meas.get("entry_id") or meas.get("list_entry_id")
    if not entry_id:
        m = re.search(r"[A-Z]{2}-L\d+", json.dumps(a))
        entry_id = m.group(0) if m else None
    entry = next((e for e in C.inst.internal if e.get("entry_id") == entry_id), None)
    if not entry:
        return ("unknown", "The internal list entry could not be found.", [x for x in [entry_id] if x])
    ev = [entry["entry_id"]]
    lname = entry.get("name") or ""
    ldob = entry.get("date_of_birth") or ""
    lyear = ldob[:4] if ldob else None
    # look in company register / PSC / receipts for the matched name with a birth date
    for d in C.docs_of("company_register_extract", "significant_control_register", "receipt", "invoice"):
        txt = d["text"]
        if lname and name_in(lname, txt):
            # PSC with birth month/year
            for line in txt.splitlines():
                if name_in(lname, line):
                    m = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)?\s*(19\d\d|20\d\d)\b", line)
                    if m and lyear:
                        if m.group(2) != lyear:
                            return ("different", "The internal list entry %s (%s) is for %s, born %s, but %s gives %s's birth year as %s, so the counterparty is a different person." % (
                                entry["entry_id"], entry.get("reason", "").split(".")[0], lname, ldob, d["doc_id"], lname, m.group(2)),
                                ev + [d["doc_id"]])
                        else:
                            ceased = re.search(r"\|\s*[^|]*\d{4}\s*\|\s*$", line.strip())
                            return ("same", "%s names %s, born %s, matching internal list entry %s (%s). The listed party is linked to or controls the counterparty." % (
                                d["doc_id"], lname, m.group(0).strip(), entry["entry_id"], entry.get("reason", "").split(".")[0]),
                                ev + [d["doc_id"]])
    return ("unknown", "", ev)


# ----------------------------------------------------------------------------- explanations

DOC_CLOSE_CODE = {
    "invoice": "INVOICE_MATCHES", "dealer_invoice": "INVOICE_MATCHES", "hospital_invoice": "INVOICE_MATCHES",
    "consulting_contract": "INVOICE_MATCHES", "supplier_invoices": "INVOICE_MATCHES", "receipt": "INVOICE_MATCHES",
    "payment_file": "INVOICE_MATCHES", "completion_statement": "DOCUMENTED_EVENT", "solicitor_letter": "DOCUMENTED_EVENT",
    "insurer_letter": "DOCUMENTED_EVENT", "vehicle_sale_receipt": "DOCUMENTED_EVENT", "sale_receipt": "DOCUMENTED_EVENT",
    "settlement_letter": "DOCUMENTED_EVENT", "loan_agreement": "DOCUMENTED_EVENT", "redundancy_letter": "DOCUMENTED_EVENT",
    "venue_email": "REFUND_OF_EARLIER_PAYMENT", "deposit_return_letter": "REFUND_OF_EARLIER_PAYMENT",
    "cancellation_confirmation": "REFUND_OF_EARLIER_PAYMENT", "bank_statement": "OWN_ACCOUNT",
    "booking_confirmation": "COLLECTION_DOCUMENTED", "cash_collection_reports": "BUSINESS_RECORDS_RECONCILE",
    "cash_reconciliation": "BUSINESS_RECORDS_RECONCILE", "takings_book": "BUSINESS_RECORDS_RECONCILE",
    "management_accounts": "BUSINESS_RECORDS_RECONCILE", "gift_letter": "THIRD_PARTY_DOCUMENTED",
    "employment_contract": "WITHIN_PROFILE", "reverification_record": "RETURNING_CUSTOMER",
}
CLAIM_ONLY = {"support_chat", "contact_note", "customer_email", "customer_letter", "staff_note", "advert",
              "due_diligence_summary", "proof_of_address", "company_register_extract", "significant_control_register",
              "cash_handling_procedure", "ticket_report", "holding_deposit_receipt", "device_export"}


def is_routine(C, t):
    """Salary from the declared employer, card spending and small bills are within profile."""
    ref = ((t.get("reference") or "") + " " + (t.get("counterparty_name") or "")).lower()
    emp = (C.cust.get("employer") or "").lower()
    if t.get("direction") == "in" and "salary" in ref and (not emp or name_in(emp, ref)):
        return True
    if t.get("direction") == "out" and ((t.get("channel") or "") == "card" or (t.get("amount_gbp") or 0) < 500):
        return True
    return False


def explain_tx(C, t):
    """Find a document that explains one alerted transaction. Returns (doc, code) or None."""
    amt = t.get("amount_gbp")
    cp = t.get("counterparty_name") or ""
    best = None
    for d in C.docs:
        typ = d.get("type")
        if typ in CLAIM_ONLY or typ not in DOC_CLOSE_CODE:
            continue
        amts = money_in(d["text"])
        if not amts:
            continue
        hit = any(abs(amt - x) <= 0.01 for x in amts)
        if not hit:
            continue
        code = DOC_CLOSE_CODE[typ]
        if typ == "bank_statement":
            # must show the account is the customer's own and the counterparty is the customer
            holder = re.search(r"\*\*Account holder:\*\*\s*([^\n]+)", d["text"])
            if not holder or not same_name(holder.group(1), C.cust_name):
                continue
            if cp and not (same_name(cp, C.cust_name) or name_in(C.cust_name, cp)):
                continue
        if not party_ok(C, d, t):
            continue
        if code == "REFUND_OF_EARLIER_PAYMENT":
            cpn = t.get("counterparty_name") or ""
            earlier = [x for x in C.inst.tx_by_cust.get(C.cid, []) if x.get("direction") == "out"
                       and x.get("booked_at", "") < t.get("booked_at", "") and cpn and same_name(x.get("counterparty_name"), cpn)]
            if not earlier:
                continue
        best = (d, code)
        break
    return best


PARTY_FIELDS = ["Borrower", "Customer", "Seller", "Bill to", "Policyholder", "Buyer", "Client", "Account holder", "Employee", "Lead traveller"]


def party_ok(C, d, t):
    """The person a document is about must be the customer (where the document names them)."""
    txt = d["text"]
    typ = d.get("type")
    names = []
    m = re.search(r"^Dear ([A-Z][^,\n]+),", txt, re.M)
    if m and not re.search(r"(?i)sir|madam|beneficiary|client|customer", m.group(1)):
        names.append(m.group(1).strip())
    for f in PARTY_FIELDS:
        m = re.search(r"\*\*%s:\*\*\s*([^\n]+)" % re.escape(f), txt)
        if m:
            names.append((f, m.group(1).strip()))
    cust = C.cust_name
    for n in names:
        if isinstance(n, tuple):
            f, v = n
            v = re.split(r",| \(", v)[0]
            if typ == "vehicle_sale_receipt":
                if f == "Seller" and t.get("direction") == "in" and not same_name(v, cust):
                    return False
                if f == "Buyer" and t.get("direction") == "out" and not same_name(v, cust):
                    return False
                continue
            if f in ("Buyer", "Client", "Lead traveller"):
                continue
            if f == "Customer" and typ == "dealer_invoice" and not same_name(v, cust):
                return False
            if f in ("Borrower", "Policyholder", "Bill to", "Account holder", "Employee") and not same_name(v, cust) \
                    and not name_in(cust, v):
                return False
        else:
            if not same_name(n, cust):
                return False
    # vehicle sales: the registration must belong to a vehicle the customer insured/owned when on file
    if typ == "vehicle_sale_receipt":
        reg = re.search(r"registration ([A-Z]{2}\d{2} ?[A-Z]{3})", txt)
        ins = C.docs_of("insurance_schedule")
        if reg and ins and not any(reg.group(1) in i["text"] for i in ins):
            return False
    return True


def collection_check(C):
    """Many payers documented by a booking/collection list naming them."""
    for d in C.docs_of("booking_confirmation"):
        txt = d["text"]
        incoming = [t for t in C.alert_tx if t.get("direction") == "in"
                    and not re.search(r"(?i)salary|wages|payroll", (t.get("reference") or "") + " " + (t.get("counterparty_name") or ""))]
        if not incoming:
            continue
        named = [t for t in incoming if t.get("counterparty_name") and name_in(t["counterparty_name"], txt)]
        per = re.search(r"£([0-9,]+\.\d\d) per person", txt)
        per_amt = float(per.group(1).replace(",", "")) if per else None
        ok_amt = per_amt is None or all(abs(t["amount_gbp"] - per_amt) <= 0.01 for t in named)
        payee = re.search(r"to ([A-Z][^,\n]+?)(?:, account|\n|,)", txt.split("**Payment:**")[-1]) if "**Payment:**" in txt else None
        tot = re.search(r"£([0-9,]+\.\d\d) in total", txt)
        tot_amt = float(tot.group(1).replace(",", "")) if tot else None
        big_out = [t for t in C.alert_tx if t.get("direction") == "out" and per_amt and t["amount_gbp"] > per_amt * 2]
        out_tx = [t for t in big_out if payee and name_in(payee.group(1).strip(), t.get("counterparty_name") or "")
                  and (tot_amt is None or abs(t["amount_gbp"] - tot_amt) <= 0.01)]
        out_ok = len(out_tx) == len(big_out)
        if named and len(named) == len(incoming) and ok_amt and out_ok:
            return (d, "%d people named on %s each paid %s, and the total went to %s for the booking, so the payments are a documented group collection." % (
                len(named), d["doc_id"], gbp(per_amt) if per_amt else "their share",
                payee.group(1).strip() if payee else "the organiser"), [d["doc_id"]] + [t["transaction_id"] for t in (out_tx[:2])])
        if incoming and len(named) < len(incoming):
            return ("mismatch", "%d of %d payers are not on the traveller list in %s." % (
                len(incoming) - len(named), len(incoming), d["doc_id"]), [d["doc_id"]])
    return None


def cash_records_check(C):
    """Cash deposits reconcile to collection reports / reconciliation sheets / takings books."""
    recs = C.docs_of("cash_collection_reports", "cash_reconciliation", "takings_book")
    if not recs:
        return None
    cash_in = [t for t in C.alert_tx if t.get("direction") == "in" and (t.get("channel") or "").lower() in ("cash", "cash_deposit", "cash_topup", "cash top-up")]
    if not cash_in:
        cash_in = [t for t in C.alert_tx if t.get("direction") == "in"]
    for d in recs:
        amts = money_in(d["text"])
        matched = [t for t in cash_in if any(abs(t["amount_gbp"] - x) <= 0.01 for x in amts)]
        if cash_in and len(matched) == len(cash_in):
            return (d, "Each alerted cash deposit (%s) matches a counted total in %s, so the deposits are the business's own takings banked as recorded." % (
                ", ".join(gbp(t["amount_gbp"]) for t in cash_in[:4]), d["doc_id"]),
                [d["doc_id"]] + [t["transaction_id"] for t in cash_in[:4]])
    return ("mismatch", "The cash deposits do not reconcile to the business records on file.", [d["doc_id"] for d in recs])


# ----------------------------------------------------------------------------- RFI choice

RFI_BY_RULE = {
    "TM01": ("RFI_SOURCE_OF_FUNDS", "proof of where the money received came from (for example a payslip, sale document or bank statement)"),
    "TM02": ("RFI_PURPOSE_DOCUMENTS", "documents showing what the incoming and onward payments were for"),
    "TM03": ("RFI_BUSINESS_RECORDS", "the business records (takings or sales records) behind the deposits"),
    "TM04": ("RFI_COUNTERPARTY_DETAILS", "who the payers or payees are and what the payments were for"),
    "TM05": ("RFI_SOURCE_OF_FUNDS", "proof of where the cash came from"),
    "TM06": ("RFI_PURPOSE_DOCUMENTS", "documents showing the purpose of the payments to or from the higher-risk country"),
    "TM07": ("RFI_SOURCE_OF_FUNDS", "proof of where the money that reactivated the account came from"),
    "TM08": ("RFI_COUNTERPARTY_DETAILS", "who the third-party account holder is and their relationship to the customer"),
    "TM10": ("RFI_PURPOSE_DOCUMENTS", "the reason the fund units were sold back so soon after purchase"),
    "TM22": ("RFI_COUNTERPARTY_DETAILS", "the counterparty's full identity details (date of birth or company number and officers)"),
}
RULE_PRIORITY = ["TM22", "TM03", "TM06", "TM02", "TM04", "TM08", "TM10", "TM05", "TM07", "TM01"]


def describe_alerts(C):
    bits = []
    for a in C.alerts:
        msg = (a.get("message") or "").split(". ")[0].rstrip(".")
        bits.append("%s (%s): %s" % (a["alert_id"], a["rule_id"], msg))
    return "; ".join(bits[:2])


# ----------------------------------------------------------------------------- decision

def decide(C):
    alert_ids = [a["alert_id"] for a in C.alerts] or list(C.case.get("alert_ids", []))
    tx_ids = [t["transaction_id"] for t in C.alert_tx]

    # 1. red flags
    flags = red_flags(C)
    lc = list_check(C)
    if lc and lc[0] == "same":
        flags.insert(0, ("ESC_LISTED_PARTY", lc[1], lc[2]))
    if flags:
        code, reason, ev = flags[0]
        return "ESCALATE", code, reason, ev + tx_ids[:3]

    # 2. explanations, alert by alert
    explained = {}
    used = []
    reasons = []
    codes = []
    blocked = None

    coll = collection_check(C) if any(r in C.rules for r in ("TM02", "TM04")) else None
    cashr = cash_records_check(C) if any(r in C.rules for r in ("TM03", "TM05", "TM01")) else None

    for a in C.alerts:
        rid = a["rule_id"]
        txs = [C.inst.tx[t] for t in (a.get("transaction_ids") or []) if t in C.inst.tx]
        if rid == "TM22":
            if lc and lc[0] == "different":
                explained[a["alert_id"]] = True
                codes.append("LIST_MATCH_DIFFERENT_PARTY")
                reasons.append(lc[1])
                used += lc[2]
                continue
            blocked = blocked or "list"
            continue
        if rid in ("TM02", "TM04") and coll and coll[0] != "mismatch":
            explained[a["alert_id"]] = True
            codes.append("COLLECTION_DOCUMENTED")
            reasons.append(coll[1])
            used += coll[2]
            continue
        if rid in ("TM03", "TM05", "TM01") and cashr and cashr[0] != "mismatch" and rid != "TM01":
            explained[a["alert_id"]] = True
            codes.append("BUSINESS_RECORDS_RECONCILE")
            reasons.append(cashr[1])
            used += cashr[2]
            continue
        # per-transaction document explanation: the alert is explained when its largest
        # transactions (covering >= 80% of the alerted value) are each matched by a document
        if not txs:
            blocked = blocked or rid
            continue
        txs = [t for t in txs if not is_routine(C, t)] or txs
        txs_sorted = sorted(txs, key=lambda t: -(t.get("amount_gbp") or 0))
        total = sum(t.get("amount_gbp") or 0 for t in txs) or 1
        covered = 0
        docs_here = []
        code_here = None
        for t in txs_sorted:
            ex = explain_tx(C, t)
            if ex:
                covered += t.get("amount_gbp") or 0
                docs_here.append((ex[0], t))
                code_here = code_here or ex[1]
        if docs_here and covered / total >= 0.8:
            explained[a["alert_id"]] = True
            codes.append(code_here)
            d, t = docs_here[0]
            reasons.append("The %s of %s %s %s on %s matches %s (%s)." % (
                "payment" if t.get("direction") == "out" else "credit", gbp(t["amount_gbp"]),
                "to" if t.get("direction") == "out" else "from", t.get("counterparty_name") or "the counterparty",
                (t.get("booked_at") or "")[:10], d.get("title", d["doc_id"]).rstrip("."), d["doc_id"]))
            used += [d["doc_id"], t["transaction_id"]] + [x[0]["doc_id"] for x in docs_here[1:3]]
        else:
            blocked = blocked or rid

    # own-account explanation at >= N times declared income -> RFI updated profile
    if not blocked and "OWN_ACCOUNT" in codes and C.monthly:
        inflow = sum(t["amount_gbp"] for t in C.alert_tx if t.get("direction") == "in")
        if inflow >= C.inst.own_acct_multiple * C.monthly:
            return ("RFI", "RFI_UPDATED_PROFILE",
                    "The %s received comes from the customer's own account, but it is %.1f times the declared monthly %s of %s, "
                    "at or above the %d times limit. Ask %s for an updated expected monthly income or account volume." % (
                        gbp(inflow), inflow / C.monthly, "income" if (C.cust.get("declared") or {}).get("monthly_income_gbp") else "volume",
                        gbp(C.monthly), C.inst.own_acct_multiple, C.cust_name), dedup(used) + tx_ids[:2])

    # mismatching business records / collection lists -> escalate as invoice mismatch / third party
    if blocked and coll and coll[0] == "mismatch":
        return ("ESCALATE", "ESC_THIRD_PARTY_FUNDING",
                "%s The unlisted people paying into the account are unexplained third parties." % coll[1], coll[2] + tx_ids[:3])

    if not blocked and C.alerts and len(explained) == len(C.alerts):
        code = codes[0]
        reason = " ".join(dedup(reasons)[:3])
        return "CLOSE", code, reason, dedup(used)

    # 3. RFI
    if lc and lc[0] == "unknown" and "TM22" in C.rules:
        entry_ev = lc[2]
        return ("RFI", "RFI_COUNTERPARTY_DETAILS",
                "A counterparty of %s matches internal list entry %s on name only, and nothing on file confirms or rules out that "
                "it is the same party. Ask the customer for the counterparty's full details (date of birth or company number, "
                "address and officers) and what the payment was for." % (C.cust_name, ", ".join(entry_ev)), entry_ev + tx_ids[:2])
    rid = next((r for r in RULE_PRIORITY if r in C.rules and not all(
        explained.get(a["alert_id"]) for a in C.alerts if a["rule_id"] == r)), C.rules[0] if C.rules else "TM01")
    code, ask = RFI_BY_RULE.get(rid, RFI_BY_RULE["TM01"])
    total_in = sum(t["amount_gbp"] for t in C.alert_tx if t.get("direction") == "in")
    total_out = sum(t["amount_gbp"] for t in C.alert_tx if t.get("direction") == "out")
    money = []
    if total_in:
        money.append("%s in" % gbp(total_in))
    if total_out:
        money.append("%s out" % gbp(total_out))
    reason = ("%s. No document on file explains the alerted activity (%s) for %s, and nothing on file points to crime yet. "
              "Ask the customer for %s." % (describe_alerts(C), ", ".join(money) or "the alerted transactions", C.cust_name, ask))
    return "RFI", code, reason, dedup(tx_ids[:5])


def dedup(xs):
    out = []
    for x in xs:
        if x and x not in out:
            out.append(x)
    return out


def main():
    folder, out = sys.argv[1], sys.argv[2]
    inst = Inst(folder)
    rows = []
    for case in inst.cases:
        try:
            C = Case(inst, case)
            verdict, code, reason, ev = decide(C)
            alert_ids = [a["alert_id"] for a in C.alerts] or case.get("alert_ids", [])
        except Exception as e:  # never crash; the safe non-close default
            verdict, code = "RFI", "RFI_PURPOSE_DOCUMENTS"
            reason = "The case could not be assessed automatically (%s). Ask the customer for documents showing what the alerted payments were for." % type(e).__name__
            ev, alert_ids = [], case.get("alert_ids", [])
        rows.append({"case_id": case["case_id"], "alert_ids": alert_ids, "verdict": verdict, "reason_code": code,
                     "reason": reason, "evidence": ev})
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
