const state = { institution: "bank", cases: [], selectedCase: null, request: 0 };
const $ = (selector) => document.querySelector(selector);

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}

function fmtDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value).slice(0, 10) : new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", year: "numeric" }).format(date);
}

function fmtMoney(value) {
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP", maximumFractionDigits: 2 }).format(number) : "—";
}

function titleCase(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function verdictClass(verdict) {
  return verdict === "CLOSE" ? "close" : verdict === "RFI" ? "rfi" : verdict === "ESCALATE" ? "escalate" : "pending";
}

function verdictText(verdict) {
  return verdict === "RFI" ? "REQUEST INFO" : verdict || "PENDING";
}

async function getJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function loadWorkspace() {
  const id = ++state.request;
  const institution = state.institution;
  const summary = await getJson(`/api/summary?institution=${encodeURIComponent(institution)}`);
  if (id !== state.request) return;
  state.summary = summary;
  const counts = summary.counts || {};
  $("#metric-total").textContent = summary.case_count.toLocaleString("en-GB");
  $("#metric-close").textContent = Number(counts.CLOSE || 0).toLocaleString("en-GB");
  $("#metric-rfi").textContent = Number(counts.RFI || 0).toLocaleString("en-GB");
  $("#metric-escalate").textContent = Number(counts.ESCALATE || 0).toLocaleString("en-GB");
  $("#nav-total").textContent = summary.case_count.toLocaleString("en-GB");
  $("#policy-name").textContent = `${titleCase(institution)} SOP`;
  $("#policy-note").textContent = institution === "payments_fx" ? "Held outgoing payments remain visible until the case has a safe disposition." : "Institution-specific thresholds and rules are applied to every case.";
  const rows = await getJson(`/api/cases?institution=${encodeURIComponent(institution)}`);
  if (id !== state.request) return;
  state.cases = rows.cases || [];
  state.selectedCase = null;
  renderQueue();
  if (state.cases.length) selectCase(state.cases[0].case_id);
  else renderEmpty("No cases in this institution.", "Choose another institution or check the data folder.");
}

function currentFiltered() {
  const query = $("#search-input").value.trim().toLowerCase();
  const verdict = $("#verdict-filter").value;
  return state.cases.filter((item) => {
    const matchesQuery = !query || [item.case_id, item.customer_name, item.customer_id, ...(item.alert_names || [])].join(" ").toLowerCase().includes(query);
    const matchesVerdict = verdict === "ALL" || item.verdict === verdict;
    return matchesQuery && matchesVerdict;
  });
}

function renderQueue() {
  const rows = currentFiltered();
  $("#queue-count").textContent = rows.length.toLocaleString("en-GB");
  $("#queue-foot-count").textContent = `${rows.length} SHOWN`;
  const list = $("#case-list");
  if (!rows.length) {
    list.innerHTML = '<div class="no-results">No cases match these filters.</div>';
    return;
  }
  list.innerHTML = rows.map((item) => {
    const cls = verdictClass(item.verdict);
    const active = state.selectedCase === item.case_id ? " selected" : "";
    const firstRule = item.alert_names && item.alert_names.length ? item.alert_names[0] : "Case review";
    return `<button class="queue-item${active}" type="button" role="option" aria-selected="${state.selectedCase === item.case_id}" data-case-id="${esc(item.case_id)}">
      <span class="queue-row"><span class="queue-case">${esc(item.case_id)}</span><span class="queue-date">${esc(fmtDate(item.opened_at))}</span></span>
      <span class="queue-customer">${esc(item.customer_name || "Unknown customer")}</span>
      <span class="queue-subrow"><span class="queue-rule">${esc(firstRule)}</span><span class="queue-alerts">${item.alert_count} alert${item.alert_count === 1 ? "" : "s"}</span></span>
      <span class="queue-row" style="margin-top:8px"><span class="verdict-pill pill-${cls}">${esc(verdictText(item.verdict))}</span>${item.held_payment ? '<span class="queue-date">HELD PAYMENT</span>' : ""}</span>
    </button>`;
  }).join("");
  list.querySelectorAll("[data-case-id]").forEach((button) => button.addEventListener("click", () => selectCase(button.dataset.caseId)));
}

function renderEmpty(title, body) {
  $("#case-content").innerHTML = `<div class="empty-state"><div class="empty-orbit"><span>◈</span></div><h2>${esc(title)}</h2><p>${esc(body)}</p></div>`;
}

async function selectCase(caseId) {
  state.selectedCase = caseId;
  renderQueue();
  $("#case-content").innerHTML = '<div class="loading-state"><span class="loader"></span>Opening case evidence…</div>';
  try {
    const data = await getJson(`/api/case/${encodeURIComponent(caseId)}?institution=${encodeURIComponent(state.institution)}`);
    if (state.selectedCase !== caseId) return;
    renderCase(data);
    $("#review-window-label").textContent = `${fmtDate(data.case.review_window && data.case.review_window.from)} – ${fmtDate(data.case.review_window && data.case.review_window.to)}`;
  } catch (error) {
    renderEmpty("Could not open this case", error.message);
  }
}

function renderCase(data) {
  const item = data.case || {};
  const customer = data.customer || {};
  const alerts = data.alerts || [];
  const transactions = data.transactions || [];
  const documents = data.documents || [];
  const decision = data.decision;
  const verdict = decision ? decision.verdict : "PENDING";
  const vclass = verdictClass(verdict);
  const evidenceIds = new Set(decision && decision.evidence || []);
  const alertTx = new Set(alerts.flatMap((alert) => alert.transaction_ids || []));
  const held = item.held_payment_id;
  const totalIn = transactions.filter((tx) => tx.direction === "in").reduce((sum, tx) => sum + Number(tx.amount_gbp || 0), 0);
  const totalOut = transactions.filter((tx) => tx.direction === "out").reduce((sum, tx) => sum + Number(tx.amount_gbp || 0), 0);
  const headerBadges = [
    `<span class="hint"><span class="hint-icon">◉</span><b>${esc(titleCase(customer.kind || "customer"))}</b></span>`,
    `<span class="hint"><span class="hint-icon">▤</span><b>${alerts.length}</b> alert${alerts.length === 1 ? "" : "s"}</span>`,
    `<span class="hint"><span class="hint-icon">↙</span> In <b>${esc(fmtMoney(totalIn))}</b></span>`,
    `<span class="hint"><span class="hint-icon">↗</span> Out <b>${esc(fmtMoney(totalOut))}</b></span>`,
  ];
  if (held) headerBadges.push('<span class="hint"><span class="hint-icon">⏸</span><b>Payment held</b></span>');
  const action = held ? verdict === "CLOSE" ? "Held payment can be released" : verdict === "RFI" ? "Keep payment held while requesting information" : "Keep payment held for MLRO review" : verdict === "CLOSE" ? "Close the case with cited evidence" : verdict === "RFI" ? "Request the specific missing information" : verdict === "ESCALATE" ? "Refer the case to the MLRO" : "Decision output is not available";

  $("#case-content").innerHTML = `
    <div class="case-header">
      <div class="case-header-top"><div><div class="case-label">CASE FILE · ${esc(titleCase(data.institution))}</div><h2 class="case-id">${esc(item.case_id)}</h2><div class="case-customer">${esc(customer.name || "Unknown customer")} <span>·</span> ${esc(customer.customer_id || "")}</div></div><div class="case-state"><span class="verdict-pill pill-${vclass}">${esc(verdictText(verdict))}</span><span class="queue-date">OPENED ${esc(fmtDate(item.opened_at))}</span></div></div>
      <div class="case-hints">${headerBadges.join("")}</div>
    </div>
    <section class="alert-section"><div class="section-title"><span>Alert rationale</span><small>${alerts.length} LINKED ALERT${alerts.length === 1 ? "" : "S"}</small></div>${alerts.map((alert) => `<article class="alert-card"><div class="alert-code">${esc(alert.rule_id || "TM")}</div><div><div class="alert-name">${esc(alert.rule_name || "Monitoring rule")}</div><div class="alert-message">${esc(alert.message || "No alert description provided.")}</div><div class="alert-transactions">${(alert.transaction_ids || []).slice(0, 8).map((id) => `<span class="tiny-ref">${esc(id)}</span>`).join("")}</div></div></article>`).join("")}</section>
    <section class="disposition disp-${vclass}"><div class="disp-top"><span class="disp-label">CASE DISPOSITION</span>${decision ? `<span class="reason-code">${esc(decision.reason_code || "NO_CODE")}</span>` : '<span class="reason-code">NO OUTPUT</span>'}</div><div class="disp-main"><span class="disp-verdict">${esc(verdict)}</span><span class="disp-action">${esc(action)}</span></div><p class="disp-reason">${esc(decision && decision.reason || "Generate the system output to display the recommended decision and case-specific rationale.")}</p>${decision ? `<div class="evidence-citations">${(decision.evidence || []).map((id) => `<span class="citation used">${esc(id)}</span>`).join("") || '<span class="citation">No cited evidence</span>'}</div>` : ""}<div class="disp-footer"><span>CASE-LEVEL DECISION</span><span>·</span><b>${alerts.length} alert${alerts.length === 1 ? "" : "s"} considered together</b></div></section>
    <div class="detail-grid"><section class="detail-section"><div class="section-title"><span>Transaction flow</span><small>${transactions.length} RECORD${transactions.length === 1 ? "" : "S"} · IN ${esc(fmtMoney(totalIn))} / OUT ${esc(fmtMoney(totalOut))}</small></div><div class="timeline">${renderTransactions(transactions, alertTx, held)}</div></section><section class="detail-section" id="evidence"><div class="section-title"><span>Evidence on file</span><small>${documents.length} DOCUMENT${documents.length === 1 ? "" : "S"}</small></div><div class="doc-list">${renderDocuments(documents, evidenceIds)}</div>${renderListMatches(data.internal_list_matches || [])}${renderPriorReviews(data.previous_reviews || [])}</section></div>`;
  $("#case-content").querySelectorAll(".doc-expand").forEach((button) => button.addEventListener("click", () => button.closest(".doc-card").classList.toggle("expanded")));
}

function renderTransactions(rows, alertTx, held) {
  if (!rows.length) return '<div class="muted-empty">No transaction records are linked to this case.</div>';
  const sorted = [...rows].sort((a, b) => String(a.booked_at || "").localeCompare(String(b.booked_at || "")));
  const linked = sorted.filter((tx) => alertTx.has(String(tx.transaction_id || "")));
  const other = sorted.filter((tx) => !alertTx.has(String(tx.transaction_id || "")));
  const visible = [...linked, ...other.slice(-Math.max(0, 28 - linked.length))].sort((a, b) => String(a.booked_at || "").localeCompare(String(b.booked_at || "")));
  return visible.map((tx) => {
    const id = String(tx.transaction_id || "");
    const direction = tx.direction === "in" ? "tx-in" : "tx-out";
    const party = tx.counterparty_name || (tx.channel === "fund_subscription" ? "Investment subscription" : "Unspecified counterparty");
    const detail = [tx.channel, tx.counterparty_country, tx.reference, id === held ? "HELD PAYMENT" : ""].filter(Boolean).join(" · ");
    return `<article class="tx-row${alertTx.has(id) ? " tx-alerted" : ""}"><div class="tx-date">${esc(fmtDate(tx.booked_at))}</div><div class="tx-main"><div class="tx-party">${esc(party)}</div><div class="tx-detail">${esc(detail)} · <span class="tiny-ref">${esc(id)}</span></div></div><div class="tx-amount ${direction}">${tx.direction === "in" ? "+" : "−"}${esc(fmtMoney(tx.amount_gbp))}</div></article>`;
  }).join("");
}

function renderDocuments(rows, evidenceIds) {
  if (!rows.length) return '<div class="muted-empty">No documents are on file.</div>';
  return rows.map((doc) => {
    const text = String(doc.text || "");
    const snippet = text.replace(/\s+/g, " ").slice(0, 180);
    return `<article class="doc-card"><div class="doc-top"><span class="doc-name">${esc(doc.title || "Evidence record")}</span><span class="doc-id">${esc(doc.id)}</span></div><div class="doc-meta"><span class="doc-type">${esc(doc.type || "document")}</span><span>${esc(fmtDate(doc.dated))}</span>${evidenceIds.has(String(doc.id)) ? '<span class="doc-used">CITED</span>' : ""}</div><div class="doc-snippet">${esc(snippet)}${text.length > 180 ? "…" : ""}</div><button class="doc-expand" type="button">${text.length > 180 ? "Read source document" : "View source"}</button><pre class="doc-full">${esc(text)}</pre></article>`;
  }).join("");
}

function renderListMatches(rows) {
  if (!rows.length) return "";
  return `<div class="extra-section"><div class="section-title"><span>Internal list match</span><small>${rows.length} ENTRY</small></div>${rows.map((row) => `<article class="doc-card"><div class="doc-top"><span class="doc-name">${esc(row.name || row.kind || "List entry")}</span><span class="doc-id">${esc(row.entry_id)}</span></div><div class="doc-snippet">${esc([row.reason, row.date_of_birth, row.address].filter(Boolean).join(" · "))}</div></article>`).join("")}</div>`;
}

function renderPriorReviews(rows) {
  if (!rows.length) return "";
  return `<div class="extra-section"><div class="section-title"><span>Prior reviews</span><small>CONTEXT ONLY</small></div>${rows.slice(0, 3).map((row) => `<article class="prior-row"><span class="doc-id">${esc(row.review_id)} · ${esc(fmtDate(row.reviewed_on))}</span><span class="prior-verdict">${esc(row.verdict)} · ${esc(row.rule_id)}</span><p>${esc(row.note || "")}</p></article>`).join("")}</div>`;
}

$("#institution-select").addEventListener("change", async (event) => {
  state.institution = event.target.value;
  $("#review-window-label").textContent = "Institution dataset";
  $("#case-list").innerHTML = '<div class="loading-state"><span class="loader"></span>Loading cases…</div>';
  try { await loadWorkspace(); } catch (error) { renderEmpty("Unable to load cases", error.message); }
});
$("#search-input").addEventListener("input", renderQueue);
$("#verdict-filter").addEventListener("change", renderQueue);
$("#refresh-button").addEventListener("click", async () => {
  state.selectedCase = null;
  $("#refresh-button").textContent = "…";
  try { await loadWorkspace(); } catch (error) { renderEmpty("Unable to refresh", error.message); }
  $("#refresh-button").textContent = "↻";
});
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    $("#search-input").focus();
  }
});

loadWorkspace().catch((error) => renderEmpty("Unable to load casework", error.message));
