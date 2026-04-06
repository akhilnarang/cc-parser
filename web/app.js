/* app.js — UI controller for cc-parser browser frontend.
 *
 * Depends on storage.js (PasswordCache, StatementStore, helpers).
 */

// ── DOM refs ────────────────────────────────────────────────
const dropZone = document.getElementById("drop-zone");
const fileInput = document.getElementById("file-input");
const fileNameEl = document.getElementById("file-name");
const controls = document.getElementById("controls");
const bankSelect = document.getElementById("bank-select");
const passwordField = document.getElementById("password-field");
const passwordInput = document.getElementById("password-input");
const parseBtn = document.getElementById("parse-btn");
const statusBar = document.getElementById("status-bar");
const statusText = document.getElementById("status-text");
const errorBar = document.getElementById("error-bar");
const errorText = document.getElementById("error-text");
const resultsEl = document.getElementById("results");
const tabBar = document.getElementById("tab-bar");
const toastContainer = document.getElementById("toast-container");
const libraryContent = document.getElementById("library-content");
const dashboardContent = document.getElementById("dashboard-content");
const settingsContent = document.getElementById("settings-content");
const libraryCountEl = document.getElementById("library-count");

// ── State ───────────────────────────────────────────────────
let pendingFile = null; // { buffer: ArrayBuffer, name: string }
let workerReady = false;
let lastResult = null;
let lastParsedResult = null; // in-memory from most recent parse
let storageAvailable = false;
let currentTab = "library";
let currentStatementId = null;

// ── Hash routing ────────────────────────────────────────────
const TAB_ROUTES = { library: "/", statement: "/statement", dashboard: "/dashboard", settings: "/settings" };
const ROUTE_TABS = { "/": "library", "/statement": "statement", "/dashboard": "dashboard", "/settings": "settings" };

function getRouteFromHash() {
  const hash = (location.hash || "#/").slice(1).replace(/\/+$/, "") || "/";
  const match = hash.match(/^\/statement\/([^/]+)$/);
  if (match) return { tab: "statement", statementId: match[1] };
  return { tab: ROUTE_TABS[hash] || "library", statementId: null };
}

function setHash(tab, statementId = null) {
  let path = TAB_ROUTES[tab] || "/";
  if (tab === "statement" && statementId) path += `/${statementId}`;
  if (location.hash !== "#" + path) location.hash = "#" + path;
}

// ── Storage instances ───────────────────────────────────────
const passwordCache = new PasswordCache();
const statementStore = new StatementStore();

// ── Worker ──────────────────────────────────────────────────
const worker = new Worker("worker.js");
showStatus("Loading Python runtime...");
worker.postMessage({ type: "init" });

// The worker.onmessage is only used during init now.
// Parse calls use the Promise-based parseOnce().
worker.onmessage = function (e) {
  const { type, text } = e.data;
  switch (type) {
    case "status":
      showStatus(text);
      break;
    case "ready":
      workerReady = true;
      hideStatus();
      if (pendingFile) parseBtn.disabled = false;
      break;
    case "error":
      hideStatus();
      showError(text);
      parseBtn.disabled = false;
      break;
  }
};

// ── Init storage ────────────────────────────────────────────
(async function initStorage() {
  const idbOk = await isIndexedDBAvailable();
  if (idbOk) {
    try {
      storageAvailable = await statementStore.init();
    } catch {
      storageAvailable = false;
    }
  }
  if (!storageAvailable) {
    showError("⚠️ Storage unavailable (private browsing?). Statement history and password caching disabled.");
    // Disable dashboard (library has its own unavailable state)
    const dashBtn = tabBar.querySelector(".tab-btn[data-tab='dashboard']");
    if (dashBtn) dashBtn.classList.add("disabled");
  } else {
    await refreshLibraryCount();
  }
  updateTabStates();

  // Route to the tab specified by the URL hash, with guards
  let route = getRouteFromHash();
  if (route.tab === "statement" && !route.statementId && !lastParsedResult) {
    route = { tab: "library", statementId: null };
  }
  if (route.tab === "dashboard" && !storageAvailable) {
    route = { tab: "library", statementId: null };
  }
  showTab(route.tab, false, route.statementId);
})();

async function refreshLibraryCount() {
  if (!storageAvailable) return;
  const stmts = await statementStore.all();
  const count = stmts.length;
  libraryCountEl.textContent = String(count);
  // Library always accessible (has its own empty state); dashboard needs data
  const hasData = count > 0;
  const dashBtn = tabBar.querySelector(".tab-btn[data-tab='dashboard']");
  if (dashBtn) dashBtn.classList.toggle("disabled", !hasData);
}

function updateTabStates() {
  // Disable "statement" tab unless there's a parsed result or a stored statement loaded
  const statementBtn = tabBar.querySelector(".tab-btn[data-tab='statement']");
  if (statementBtn) statementBtn.classList.toggle("disabled", !lastParsedResult && !lastResult);
}

// ── parseOnce: Promise wrapper for one worker round-trip ────
//
// Known limitation: the worker doesn't echo a request ID, so after a
// timeout the late reply is indistinguishable from a fresh one. In theory
// a timeout → retry could resolve the new Promise with stale data.
// In practice this cannot happen: statements are ≤5 pages and parse in
// 1-3 seconds, so the 60s timeout is unreachable. If future use cases
// involve much larger PDFs, add a request ID to the worker protocol.
let parseRequestId = 0;
function parseOnce(buffer, filename, password, bank) {
  const reqId = ++parseRequestId;
  return new Promise((resolve) => {
    let resolved = false;
    const done = (data) => {
      if (resolved) return;
      resolved = true;
      worker.removeEventListener("message", handler);
      resolve(data);
    };
    const handler = (e) => {
      const { type } = e.data;
      // Discard replies if a newer parseOnce call has been made (e.g. concurrent click).
      if (reqId !== parseRequestId) { done({ type: "error", text: "Superseded" }); return; }
      if (type === "result" || type === "encrypted" || type === "wrong_password" || type === "error") {
        done(e.data);
      }
    };
    worker.addEventListener("message", handler);
    worker.postMessage({
      type: "parse",
      payload: { pdfBuffer: buffer, filename, password, bank },
    });
    setTimeout(() => done({ type: "error", text: "Parse timeout (60s)" }), 60000);
  });
}

// Note: formatDate is defined near the bottom of this file alongside other
// formatting helpers (formatAmount, formatMonth, formatBytes, etc.).

// ── File handling ───────────────────────────────────────────
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) handleFile(fileInput.files[0]);
});

dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.classList.add("dragover");
});

dropZone.addEventListener("dragleave", () => {
  dropZone.classList.remove("dragover");
});

dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("dragover");
  if (e.dataTransfer.files.length) {
    fileInput.files = e.dataTransfer.files;
    handleFile(e.dataTransfer.files[0]);
  }
});

function handleFile(file) {
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    showError("Please select a PDF file.");
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    pendingFile = { buffer: reader.result, name: file.name };
    dropZone.classList.add("has-file");
    fileNameEl.textContent = file.name;
    controls.classList.add("visible");
    resultsEl.classList.remove("visible");
    passwordInput.value = "";
    passwordField.classList.remove("visible");
    hideError();
    parseBtn.disabled = !workerReady;
  };
  reader.readAsArrayBuffer(file);
}

// ── Parse with auto-retry ───────────────────────────────────
parseBtn.addEventListener("click", () => triggerParse());

passwordInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !parseBtn.disabled) triggerParse();
});

async function triggerParse() {
  if (!pendingFile || !workerReady) return;
  hideError();
  resultsEl.classList.remove("visible");
  parseBtn.disabled = true;
  showStatus("Parsing PDF...");

  const buffer = pendingFile.buffer;
  const filename = pendingFile.name;
  const bank = bankSelect.value;
  const manualPassword = passwordInput.value || null;

  let result;

  if (manualPassword) {
    // User provided a password directly — try it
    result = await parseOnce(buffer, filename, manualPassword, bank);
    if (result.type === "result") {
      const resolvedBank = (result.data.bank || bank).toLowerCase();
      passwordCache.set(resolvedBank, manualPassword);
    } else if (result.type === "wrong_password") {
      hideStatus();
      passwordField.classList.add("visible");
      passwordInput.focus();
      showError("Wrong password. Please try again.");
      parseBtn.disabled = false;
      return;
    }
  } else {
    // Step 1: try without password
    result = await parseOnce(buffer, filename, null, bank);

    if (result.type === "encrypted") {
      // Step 2: try cached password for selected bank
      const bankKey = bank !== "auto" ? bank : null;
      let tried = new Set();

      if (bankKey && passwordCache.get(bankKey)) {
        showStatus(`Trying stored password for ${bankKey.toUpperCase()}...`);
        const pw = passwordCache.get(bankKey);
        result = await parseOnce(buffer, filename, pw, bank);
        tried.add(bankKey);
      }

      // Step 3: try all cached banks (max 3 attempts)
      if (result.type === "encrypted" || result.type === "wrong_password") {
        const candidates = passwordCache.banks().filter((b) => !tried.has(b));
        let attempts = 0;
        for (const candidateBank of candidates) {
          if (attempts >= 3) break;
          showStatus(`Trying stored password for ${candidateBank.toUpperCase()}...`);
          const pw = passwordCache.get(candidateBank);
          result = await parseOnce(buffer, filename, pw, bank);
          attempts++;
          tried.add(candidateBank);
          if (result.type === "result") {
            const resolvedBank = (result.data.bank || candidateBank).toLowerCase();
            if (resolvedBank !== candidateBank) {
              // Cross-bank password match — warn user
              showToast(`⚠️ Password from ${candidateBank.toUpperCase()} worked on ${resolvedBank.toUpperCase()} PDF. Be careful!`);
              passwordCache.set(resolvedBank, pw);
            }
            break;
          }
          if (result.type === "error") break;
        }
      }

      // Step 4: all auto-retries failed → show password field
      if (result.type === "encrypted" || result.type === "wrong_password") {
        hideStatus();
        passwordField.classList.add("visible");
        passwordInput.focus();
        const msg = result.type === "wrong_password"
          ? "Stored passwords didn't work. Enter the correct password."
          : "PDF is encrypted. Enter the password and try again.";
        showError(msg);
        parseBtn.disabled = false;
        return;
      }
    }

    // wrong_password without encrypted step (password was given but wrong)
    if (result.type === "wrong_password") {
      hideStatus();
      passwordField.classList.add("visible");
      passwordInput.focus();
      showError("Wrong password. Please try again.");
      parseBtn.disabled = false;
      return;
    }
  }

  // Handle final result
  hideStatus();

  if (result.type === "error") {
    showError(result.text);
    parseBtn.disabled = false;
    return;
  }

  if (result.type === "result") {
    lastParsedResult = result.data;
    lastResult = result.data;
    updateTabStates();
    showTab("statement");

    // Cache password on success (if manually entered)
    if (manualPassword) {
      const resolvedBank = (result.data.bank || bank).toLowerCase();
      passwordCache.set(resolvedBank, manualPassword);
      showToast(`Password cached for ${resolvedBank.toUpperCase()} (session only)`);
      passwordInput.value = "";
    }

    // Auto-store to IndexedDB
    if (storageAvailable) {
      try {
        const record = await statementStore.put(result.data, buffer);
        await refreshLibraryCount();
        if (currentTab === "library") renderLibrary();
        const bankLabel = (record.bank || "").toUpperCase();
        const cardLabel = record.card_last_four !== "unknown" ? ` ...${record.card_last_four}` : "";
        const dueLabel = record.due_date !== "unknown" ? `, due ${formatDate(record.due_date)}` : "";
        
        // Warn if card or date is unknown
        if (record.card_last_four === "unknown" || record.due_date === "unknown") {
          showToast(`⚠️ Statement saved with incomplete info (card/date unknown)`);
        }
        
        showToast(
          `Statement saved (${bankLabel}${cardLabel}${dueLabel})`,
          "Undo",
          async () => {
            await statementStore.delete(record.id);
            await refreshLibraryCount();
            if (currentTab === "library") renderLibrary();
          },
        );
      } catch (err) {
        if (err.name === "QuotaExceededError") {
          showToast("Storage full. Delete old statements in Settings.");
        } else {
          showToast(`⚠️ Auto-store failed: ${err.message}`);
          console.warn("Auto-store failed:", err);
        }
      }
    }

    parseBtn.disabled = false;
    return;
  }

  // Unexpected
  showError("Unexpected response from parser.");
  parseBtn.disabled = false;
}

// ── Tab system + hash routing ──────────────────────────────
tabBar.addEventListener("click", (e) => {
  const btn = e.target.closest(".tab-btn");
  if (!btn || btn.classList.contains("disabled")) return;
  showTab(btn.dataset.tab);
});

window.addEventListener("hashchange", () => {
  let route = getRouteFromHash();
  if (route.tab === "statement" && !route.statementId && !lastParsedResult) {
    route = { tab: "library", statementId: null };
  }
  if (route.tab === "dashboard" && !storageAvailable) {
    route = { tab: "library", statementId: null };
  }
  if (route.tab !== currentTab || route.statementId !== currentStatementId) {
    showTab(route.tab, true, route.statementId);
  }
});

let navCounter = 0; // guard against stale async completions

async function showTab(tab, skipHash = false, statementId = null) {
  const thisNav = ++navCounter;
  currentTab = tab;
  currentStatementId = tab === "statement" ? statementId : null;
  if (!skipHash) setHash(tab, statementId);
  // Update tab buttons
  for (const btn of tabBar.querySelectorAll(".tab-btn")) {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  }
  // Update tab panes
  for (const pane of document.querySelectorAll(".tab-pane")) {
    pane.classList.toggle("active", pane.dataset.pane === tab);
  }
  // Render tab content
  if (tab === "library") { renderLibrary(); return; }
  if (tab === "dashboard") { renderDashboard(); return; }
  if (tab === "settings") { renderSettings(); return; }
  if (tab === "statement") {
    if (statementId) {
      if (!storageAvailable) {
        showError("Storage unavailable. Cannot open saved statement.");
        location.replace("#/");
        return;
      }
      const record = await statementStore.get(statementId);
      if (thisNav !== navCounter) return; // stale navigation
      if (!record) {
        showError("Statement not found.");
        location.replace("#/");
        return;
      }
      lastResult = record.data;
      updateTabStates();
      renderResults(record.data);
      return;
    }
    if (lastParsedResult) {
      lastResult = lastParsedResult;
      renderResults(lastParsedResult);
      return;
    }
    // No data — replace hash to avoid back-button loop
    location.replace("#/");
  }
}

// ── Toast system ────────────────────────────────────────────
function showToast(message, actionLabel, actionFn) {
  const el = document.createElement("div");
  el.className = "toast";
  let html = `<span class="toast-msg">${esc(message)}</span>`;
  if (actionLabel) {
    html += `<button class="toast-action">${esc(actionLabel)}</button>`;
  }
  el.innerHTML = html;

  if (actionLabel && actionFn) {
    el.querySelector(".toast-action").addEventListener("click", () => {
      actionFn();
      el.remove();
    });
  }

  toastContainer.appendChild(el);

  // Auto-dismiss after 5s
  setTimeout(() => {
    el.classList.add("fade-out");
    el.addEventListener("animationend", () => el.remove());
  }, 5000);
}

// ── Status / error helpers ──────────────────────────────────
function showStatus(msg) {
  statusText.textContent = msg;
  statusBar.classList.add("visible");
  hideError();
}
function hideStatus() {
  statusBar.classList.remove("visible");
}
function showError(msg) {
  errorText.textContent = msg;
  errorBar.classList.add("visible");
}
function hideError() {
  errorBar.classList.remove("visible");
}

// ── Library tab ─────────────────────────────────────────────
async function renderLibrary() {
  if (!storageAvailable) {
    libraryContent.innerHTML = `<div class="storage-unavailable">
      <p>Storage unavailable in private browsing mode.</p></div>`;
    return;
  }

  const stmts = await statementStore.all();
  if (stmts.length === 0) {
    libraryContent.innerHTML = `<div class="library-empty">
      <p>No statements stored yet. Parse a PDF to get started.</p></div>`;
    return;
  }

  // Sort by due_date descending, unknowns last
  const sortKey = (s) => s.due_date && s.due_date !== "unknown" ? s.due_date : "0000-00-00";
  stmts.sort((a, b) => sortKey(b).localeCompare(sortKey(a)));

  // Group by month
  const groups = new Map();
  for (const s of stmts) {
    const month = s.due_date && s.due_date !== "unknown" ? s.due_date.slice(0, 7) : "unknown";
    if (!groups.has(month)) groups.set(month, []);
    groups.get(month).push(s);
  }

  let html = `<div class="results-header fade-settle"><h2 class="results-title">Library</h2>`;
  html += `<span class="table-title"><span class="badge">${stmts.length} statement${stmts.length !== 1 ? "s" : ""}</span></span>`;
  html += `</div>`;

  for (const [month, items] of groups) {
    const monthLabel = month !== "unknown" ? formatMonth(month) : "Unknown Date";
    html += `<div class="lib-month-group fade-settle-1">`;
    html += `<h3 class="lib-month-heading">${esc(monthLabel)}</h3>`;
    html += `<div class="lib-cards">`;
    for (const s of items) {
      const bank = (s.bank || "").toUpperCase();
      const card = s.card_last_four !== "unknown" ? `...${s.card_last_four}` : "";
      const dueDate = s.due_date !== "unknown" ? formatDate(s.due_date) : "-";
      html += `<div class="lib-card">`;
      html += `<div class="lib-card-main">`;
      html += `<div class="lib-card-bank">${esc(bank)}</div>`;
      html += `<div class="lib-card-amount">${esc(s.overall_total || "0.00")}</div>`;
      html += `</div>`;
      html += `<div class="lib-card-meta">`;
      if (card) html += `<span>${esc(card)}</span>`;
      html += `<span>${esc(dueDate)}</span>`;
      if (s.name) html += `<span>${esc(s.name)}</span>`;
      html += `</div>`;
      html += `<div class="lib-card-actions">`;
      html += `<button class="btn btn-secondary btn-sm lib-view" data-id="${s.id}">View</button>`;
      html += `<button class="btn btn-danger btn-sm lib-delete" data-id="${s.id}">Delete</button>`;
      html += `</div>`;
      html += `</div>`;
    }
    html += `</div></div>`;
  }

  // Footer buttons
  html += `<div class="export-bar">`;
  html += `<button class="btn btn-danger btn-sm" id="lib-clear-all">Clear All Statements</button>`;
  html += `<button class="btn btn-secondary" id="lib-export-json">Export All JSON</button>`;
  html += `<button class="btn btn-secondary" id="lib-export-csv">Export All CSV</button>`;
  html += `</div>`;

  libraryContent.innerHTML = html;

  // Wire up view/delete
  for (const btn of libraryContent.querySelectorAll(".lib-view")) {
    btn.addEventListener("click", () => {
      showTab("statement", false, btn.dataset.id);
    });
  }

  for (const btn of libraryContent.querySelectorAll(".lib-delete")) {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.id;
      const record = await statementStore.get(id);
      
      // Confirmation before delete
      const bankLabel = (record?.bank || "").toUpperCase();
      const cardLabel = record?.card_last_four && record.card_last_four !== "unknown" ? ` ...${record.card_last_four}` : "";
      if (!confirm(`Delete ${bankLabel}${cardLabel} statement?`)) {
        return;
      }
      
      await statementStore.delete(id);
      await refreshLibraryCount();
      renderLibrary();
      if (record) {
        showToast(`Deleted ${bankLabel} statement`, "Undo", async () => {
          await statementStore._tx("readwrite", (store) => store.put(record));
          await refreshLibraryCount();
          if (currentTab === "library") renderLibrary();
        });
      }
    });
  }

  // Wire up export buttons and clear-all
  const clearAllBtn = document.getElementById("lib-clear-all");
  const exportJsonBtn = document.getElementById("lib-export-json");
  const exportCsvBtn = document.getElementById("lib-export-csv");
  
  if (clearAllBtn) {
    clearAllBtn.addEventListener("click", async () => {
      if (!confirm("Delete all statements? This cannot be undone.")) return;
      await statementStore.deleteAll();
      await refreshLibraryCount();
      renderLibrary();
      showToast("All statements deleted");
    });
  }
  if (exportJsonBtn) {
    exportJsonBtn.addEventListener("click", async () => {
      const all = await statementStore.all();
      const data = all.map((s) => s.data);
      download(
        new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
        "cc-parser-all-statements.json",
      );
    });
  }
  if (exportCsvBtn) {
    exportCsvBtn.addEventListener("click", async () => {
      const all = await statementStore.all();
      exportAllCSV(all);
    });
  }
}

function exportAllCSV(stmts) {
  const fields = [
    "bank", "file", "source", "transaction_type",
    "person", "card_number", "date", "time", "narration", "reward_points",
    "amount", "amount_numeric", "signed_amount", "spend_amount", "credit_amount",
  ];
  const rows = [];

  for (const s of stmts) {
    const d = s.data;
    function addRow(source, txn) {
      const amount = parseFloat(String(txn.amount || "0").replace(/,/g, "")) || 0;
      const txnType = txn.transaction_type || "";
      const isCredit = source === "payments_refunds" || txnType === "credit";
      rows.push({
        bank: d.bank || "", file: d.file || "", source,
        transaction_type: txnType,
        person: txn.person || "", card_number: txn.card_number || "",
        date: txn.date || "", time: txn.time || "",
        narration: txn.narration || "", reward_points: txn.reward_points || "",
        amount: String(txn.amount || "0"),
        amount_numeric: amount.toFixed(2),
        signed_amount: (isCredit ? -amount : amount).toFixed(2),
        spend_amount: (source === "transactions" ? amount : 0).toFixed(2),
        credit_amount: (isCredit ? amount : 0).toFixed(2),
      });
    }
    (d.transactions || []).forEach((t) => addRow("transactions", t));
    (d.payments_refunds || []).forEach((t) => addRow("payments_refunds", t));
  }

  let csv = fields.join(",") + "\n";
  for (const row of rows) {
    csv += fields.map((f) => csvCell(row[f])).join(",") + "\n";
  }
  download(new Blob([csv], { type: "text/csv" }), "cc-parser-all-statements.csv");
}

// ── Dashboard tab ───────────────────────────────────────────
async function renderDashboard() {
  if (!storageAvailable) {
    dashboardContent.innerHTML = `<div class="storage-unavailable">
      <p>Storage unavailable in private browsing mode.</p></div>`;
    return;
  }

  const stmts = await statementStore.all();
  if (stmts.length === 0) {
    dashboardContent.innerHTML = `<div class="library-empty">
      <p>No statements stored yet. Parse some PDFs first.</p></div>`;
    return;
  }

  const byMonth = await statementStore.aggregateByMonth(stmts);
  const byPerson = await statementStore.aggregateByPersonCard(stmts);
  const totalPoints = await statementStore.totalRewardPoints(stmts);
  const latestPoints = await statementStore.latestRewardPoints(stmts);

  // Compute summary
  let totalSpend = 0;
  const cards = new Set();
  const dates = [];
  for (const s of stmts) {
    totalSpend += StatementStore.parseAmount(s.overall_total);
    cards.add(`${s.bank}|${s.card_last_four}`);
    if (s.due_date !== "unknown") dates.push(s.due_date);
  }
  dates.sort();
  const dateRange = dates.length > 0 ? `${formatDate(dates[0])} to ${formatDate(dates[dates.length - 1])}` : "-";

  let html = "";

  // Dashboard header
  html += `<div class="results-header fade-settle">`;
  html += `<h2 class="results-title">Dashboard</h2>`;
  html += `</div>`;

  html += `<div class="content-grid">`;
  html += `<div>`;
  html += `<div class="summary-grid summary-grid--sidebar">`;
  html += summaryCard("Total Spend", formatAmount(totalSpend), "hero");
  html += summaryCard("Statements", String(stmts.length));
  html += summaryCard("Cards", String(cards.size));
  html += summaryCard("Date Range", dateRange);
  if (totalPoints > 0) {
    html += summaryCard("Total Reward Points", formatNumber(totalPoints));
    html += summaryCard("Latest Points Balance", formatNumber(latestPoints));
  }
  html += `</div></div>`;
  html += `<div>`;

  // Spend by Month (vertical bar chart, fallback to horizontal for >12)
  if (byMonth.length > 0) {
    const maxMonth = Math.max(...byMonth.map((m) => m.total));
    html += `<div class="dash-section"><h3>Spend by Month</h3>`;

    if (byMonth.length <= 12) {
      // Vertical chart
      html += `<div class="chart-container">`;
      for (const m of byMonth) {
        const pct = maxMonth > 0 ? (m.total / maxMonth) * 100 : 0;
        const barPct = pct > 0 ? Math.max(pct, 3) : 0;
        const label = m.month !== "unknown" ? formatMonth(m.month) : "?";
        html += `<div class="chart-col">`;
        html += `<div class="chart-amount">${esc(formatAmount(m.total))}</div>`;
        html += `<div class="chart-bar-wrap"><div class="chart-bar" style="height:${barPct.toFixed(1)}%"></div></div>`;
        html += `<div class="chart-label">${esc(label)}</div>`;
        html += `</div>`;
      }
      html += `</div>`;
    } else {
      // Fallback horizontal bars
      for (const m of byMonth) {
        const pct = maxMonth > 0 ? (m.total / maxMonth) * 100 : 0;
        const label = m.month !== "unknown" ? formatMonth(m.month) : "Unknown";
        html += `<div class="bar-row">`;
        html += `<div class="bar-label">${esc(label)}</div>`;
        html += `<div class="bar-track"><div class="bar-fill" style="width:${pct.toFixed(1)}%"></div></div>`;
        html += `<div class="bar-value">${esc(formatAmount(m.total))}</div>`;
        html += `</div>`;
      }
    }
    html += `</div>`;
  }

  // Spend by Person/Card
  if (byPerson.length > 0) {
    html += tableSection(
      "Spend by Person / Card",
      null,
      ["Person", "Card", "Txns", "Total Spend"],
      byPerson.map((p) => [
        { v: p.name || "-" },
        { v: p.card !== "unknown" ? `...${p.card}` : "-", c: "col-date" },
        { v: String(p.txns), c: "col-count" },
        { v: formatAmount(p.total), c: "col-amount" },
      ]),
    );
  }

  html += `</div></div>`; // close main + content-grid
  dashboardContent.innerHTML = html;
}

// ── Settings tab ────────────────────────────────────────────
async function renderSettings() {
  let html = "";

  // Password cache section
  html += `<div class="settings-group">`;
  html += `<h3>In-Memory Password Cache</h3>`;
  const cachedBanks = passwordCache.banks();
  if (cachedBanks.length === 0) {
    html += `<p class="settings-note">No passwords cached in this session.</p>`;
  } else {
    html += `<p style="font-size:0.87rem;margin-bottom:8px">Session passwords cached for: <strong>${esc(cachedBanks.map((b) => b.toUpperCase()).join(", "))}</strong></p>`;
    html += `<p class="settings-note">Cleared when you close this tab.</p>`;
    for (const b of cachedBanks) {
      html += `<button class="btn btn-secondary btn-sm pw-clear-bank" data-bank="${esc(b)}" style="margin-right:6px;margin-top:6px">Clear ${esc(b.toUpperCase())}</button>`;
    }
    html += `<br>`;
  }
  html += `<button class="btn btn-secondary btn-sm" id="pw-clear-all" style="margin-top:12px">Clear All Passwords</button>`;
  html += `</div>`;

  // Storage section
  html += `<div class="settings-group">`;
  html += `<h3>Storage Usage</h3>`;
  if (!storageAvailable) {
    html += `<p class="settings-note">IndexedDB is unavailable (possibly private browsing).</p>`;
  } else {
    const usage = await statementStore.storageUsage();

    html += `<div class="settings-row">`;
    html += `<span>${usage.count} statement${usage.count !== 1 ? "s" : ""} stored &middot; ${formatBytes(usage.bytes)}</span>`;
    html += `</div>`;

    html += `<button class="btn btn-danger btn-sm" id="clear-all-stmts" style="margin-top:12px">Clear All Statements</button>`;
    html += `<button class="btn btn-secondary btn-sm" id="reset-db" style="margin-top:12px;margin-left:6px">Reset Database</button>`;
  }
  html += `</div>`;

  // Browser storage detection
  html += `<div class="settings-group">`;
  html += `<h3>Browser Storage</h3>`;
  html += `<div class="detect-item ${storageAvailable ? "detect-ok" : "detect-fail"}">`;
  html += storageAvailable ? "✓ IndexedDB: Available" : "✗ IndexedDB: Unavailable";
  html += `</div>`;
  if (isMobileSafari()) {
    html += `<div class="detect-item detect-warn">⚠️ Mobile Safari detected — 50MB storage limit. Export regularly.</div>`;
  }
  html += `<p class="settings-note" style="margin-top:8px">⚠️ Statements are stored unencrypted in your browser. Keep your device secure.</p>`;
  html += `</div>`;

  settingsContent.innerHTML = html;

  // Wire up buttons
  for (const btn of settingsContent.querySelectorAll(".pw-clear-bank")) {
    btn.addEventListener("click", () => {
      passwordCache.delete(btn.dataset.bank);
      renderSettings();
      showToast(`Password cleared for ${btn.dataset.bank.toUpperCase()}`);
    });
  }

  const pwClearAll = document.getElementById("pw-clear-all");
  if (pwClearAll) {
    pwClearAll.addEventListener("click", () => {
      passwordCache.clear();
      renderSettings();
      showToast("All cached passwords cleared");
    });
  }

  const clearAllBtn = document.getElementById("clear-all-stmts");
  if (clearAllBtn) {
    clearAllBtn.addEventListener("click", async () => {
      if (!confirm("Delete ALL stored statements? This cannot be undone.")) return;
      await statementStore.deleteAll();
      await refreshLibraryCount();
      renderSettings();
      showToast("All statements deleted");
    });
  }

  const resetDbBtn = document.getElementById("reset-db");
  if (resetDbBtn) {
    resetDbBtn.addEventListener("click", async () => {
      if (!confirm("Reset database? All statements will be deleted and database recreated.")) return;
      try {
        await statementStore.deleteAll();
        // Close and reopen DB to reset
        statementStore._db?.close();
        statementStore._db = null;
        await statementStore.init();
        await refreshLibraryCount();
        renderSettings();
        showToast("Database reset successfully");
      } catch (err) {
        showError(`Failed to reset database: ${err.message}`);
      }
    });
  }
}

// ── Tab close: clear password cache (best effort) ───────────
window.addEventListener("beforeunload", () => {
  passwordCache.clear();
});

// ── Render results (Current tab) ────────────────────────────
function renderResults(d) {
  let html = "";

  // Asymmetric grid: sidebar (summary) + main (tables)
  html += `<div class="results-header fade-settle">`;
  html += `<h2 class="results-title">Parsed Statement</h2>`;
  html += `</div>`;
  html += `<div class="content-grid">`;
  html += `<div class="fade-settle-1">`;
  html += `<div class="summary-grid summary-grid--sidebar">`;
  html += summaryCard("Total Due", d.statement_total_amount_due || "-", "hero");
  html += summaryCard("Spend Total", d.overall_total || "0.00", "hero");
  html += summaryCard("Bank", (d.bank || "-").toUpperCase());
  html += summaryCard("Name", d.name || "-");
  html += summaryCard("Card", d.card_number || "-");
  html += summaryCard("Due Date", d.due_date || "-");
  html += summaryCard("Reward Points", d.overall_reward_points || "0");
  if (d.reward_points_balance) {
    html += summaryCard("Points Balance", d.reward_points_balance);
  }
  html += `</div></div>`;
  html += `<div class="fade-settle-2">`;

  // Payments / Refunds
  if (d.payments_refunds && d.payments_refunds.length) {
    html += tableSection(
      "Payments / Refunds",
      d.payments_refunds.length,
      ["Date", "Time", "Person", "Narration", "Amount"],
      d.payments_refunds.map((t) => {
        const row = [
          { v: t.date, c: "col-date" },
          { v: t.time || "", c: "col-time" },
          { v: t.person || "" },
          { v: t.narration, c: "col-narration" },
          { v: t.amount, c: "col-amount" },
        ];
        row.rowClass = "is-credit";
        return row;
      }),
    );
    html += `<div class="table-group-total">Total: ${d.payments_refunds_total || "0.00"}</div>`;
  }

  // Adjustment Pairs (two-row layout)
  const adjPairs = d.possible_adjustment_pairs || [];
  if (adjPairs.length) {
    html += `<div class="table-section">`;
    html += `<h3 class="table-title">${esc("Adjustment Pairs")} <span class="badge">${adjPairs.length}</span></h3>`;
    html += `<div class="table-wrap"><table><thead><tr>`;
    html += `<th>Kind</th><th>Confidence</th><th>Date</th><th>Side</th><th class="col-amount">Amount</th><th>Narration</th><th class="col-amount">Delta</th>`;
    html += `</tr></thead>`;
    for (const p of adjPairs) {
      const hasCr = p.credit != null;
      const rs = hasCr ? ' rowspan="2"' : "";
      const confCls = p.confidence === "high" ? "is-credit" : p.confidence === "low" ? "is-debit" : "";
      html += `<tbody class="adj-pair-group">`;
      html += `<tr>`;
      html += `<td${rs}>${esc(p.kind || "")}</td>`;
      html += `<td${rs} class="${confCls}">${esc(p.confidence || "")}</td>`;
      html += `<td class="col-date">${esc(p.debit ? p.debit.date : "—")}</td>`;
      html += `<td class="is-debit">debit</td>`;
      html += `<td class="col-amount is-debit">${p.debit ? "₹" + esc(p.debit.amount) : "—"}</td>`;
      html += `<td class="col-narration">${esc(p.debit ? p.debit.narration : "—")}</td>`;
      html += `<td${rs} class="col-amount">${"₹" + esc(p.amount_delta || "0.00")}</td>`;
      html += `</tr>`;
      if (hasCr) {
        html += `<tr>`;
        html += `<td class="col-date">${esc(p.credit.date)}</td>`;
        html += `<td class="is-credit">credit</td>`;
        html += `<td class="col-amount is-credit">₹${esc(p.credit.amount)}</td>`;
        html += `<td class="col-narration">${esc(p.credit.narration)}</td>`;
        html += `</tr>`;
      }
      html += `</tbody>`;
    }
    html += `</table></div></div>`;
  }

  // Transactions by person group
  if (d.person_groups && d.person_groups.length) {
    for (const g of d.person_groups) {
      const hasRewards = g.transactions.some(
        (t) => t.reward_points && !["0", "0.0", "0.00", ""].includes(String(t.reward_points).trim()),
      );
      const cols = ["Date", "Time", "Narration"];
      if (hasRewards) cols.push("Reward Pts");
      cols.push("Amount");

      html += tableSection(
        `Transactions — ${g.person || "Unknown"}`,
        g.transaction_count,
        cols,
        g.transactions.map((t) => {
          const row = [
            { v: t.date, c: "col-date" },
            { v: t.time || "", c: "col-time" },
            { v: t.narration, c: "col-narration" },
          ];
          if (hasRewards) row.push({ v: t.reward_points || "", c: "col-points" });
          row.push({ v: t.amount, c: "col-amount" });
          return row;
        }),
      );
      html += `<div class="table-group-total">Total: ${g.total_amount || "0.00"} &middot; Points: ${g.reward_points_total || "0"}</div>`;
    }
  } else if (d.transactions && d.transactions.length) {
    const hasRewards = d.transactions.some(
      (t) => t.reward_points && !["0", "0.0", "0.00", ""].includes(String(t.reward_points).trim()),
    );
    const cols = ["Date", "Time", "Person", "Narration"];
    if (hasRewards) cols.push("Reward Pts");
    cols.push("Amount");

    html += tableSection(
      "Transactions",
      d.transactions.length,
      cols,
      d.transactions.map((t) => {
        const row = [
          { v: t.date, c: "col-date" },
          { v: t.time || "", c: "col-time" },
          { v: t.person || "" },
          { v: t.narration, c: "col-narration" },
        ];
        if (hasRewards) row.push({ v: t.reward_points || "", c: "col-points" });
        row.push({ v: t.amount, c: "col-amount" });
        return row;
      }),
    );
  }

  // Card summaries
  if (d.card_summaries && d.card_summaries.length) {
    html += tableSection(
      "Totals by Person / Card",
      null,
      ["Person", "Card", "Txns", "Points", "Total"],
      d.card_summaries.map((s) => [
        { v: s.person || "" },
        { v: s.card_number || "", c: "col-date" },
        { v: String(s.transaction_count), c: "col-count" },
        { v: s.reward_points_total || "0", c: "col-points" },
        { v: s.total_amount || "0.00", c: "col-amount" },
      ]),
    );
  }

  // Reconciliation — Confidence Panel
  if (d.reconciliation) {
    const r = d.reconciliation;
    const delta = parseFloat(String(r.smart_delta || "0").replace(/,/g, "")) || 0;
    const absDelta = Math.abs(delta);
    const confidence = absDelta === 0 ? "balanced" : absDelta < 1 ? "minor" : "review";
    const confidenceLabel = { balanced: "Balanced", minor: "Minor delta", review: "Needs review" }[confidence];

    const rows = [
      ["Statement Total Due", r.statement_total_amount_due],
      ["Previous Balance", r.header_previous_balance],
      ["Parsed Debit Total", r.parsed_debit_total],
      ["Parsed Credit Total", r.parsed_credit_total],
      ["Smart Expected Total", r.smart_expected_total],
      ["Smart Delta", r.smart_delta],
    ];
    if (r.prev_balance_cleared_date) {
      rows.push(["Prev Balance Cleared On", r.prev_balance_cleared_date]);
      rows.push(["Excess Paid After Clearing", r.excess_paid_after_clearing || "0.00"]);
    }
    rows.push(["Delta (Statement vs Net)", r.delta_statement_vs_parsed_net]);

    html += `<div class="table-section recon-panel">`;
    html += `<div class="recon-header">`;
    html += `<h3 class="table-title">Reconciliation</h3>`;
    html += `<span class="confidence-pill confidence-${confidence}">${confidenceLabel}</span>`;
    html += `</div>`;
    html += `<div class="recon-hero-delta">${esc(r.smart_delta || "0.00")}</div>`;
    html += `<div class="recon-grid">`;
    for (const [k, v] of rows) {
      html += `<div class="recon-row"><div class="recon-key">${esc(k)}</div><div class="recon-val">${esc(v || "-")}</div></div>`;
    }
    html += `</div></div>`;
  }

  html += `</div></div>`; // close main + content-grid

  // Export bar
  html += `<div class="export-bar">`;
  html += `<button class="btn btn-secondary" id="export-json">Download JSON</button>`;
  html += `<button class="btn btn-secondary" id="export-csv">Download CSV</button>`;
  html += `</div>`;

  resultsEl.innerHTML = html;
  resultsEl.classList.add("visible");

  document.getElementById("export-json").addEventListener("click", exportJSON);
  document.getElementById("export-csv").addEventListener("click", exportCSV);
}

// ── Table builder ───────────────────────────────────────────
function tableSection(title, count, columns, rows) {
  let h = `<div class="table-section">`;
  h += `<h3 class="table-title">${esc(title)}`;
  if (count != null) h += ` <span class="badge">${count}</span>`;
  h += `</h3>`;
  h += `<div class="table-wrap"><table><thead><tr>`;
  for (const col of columns) h += `<th>${esc(col)}</th>`;
  h += `</tr></thead><tbody>`;
  for (const row of rows) {
    const rc = row.rowClass ? ` class="${row.rowClass}"` : "";
    h += `<tr${rc}>`;
    for (const cell of row) {
      const cls = cell.c ? ` class="${cell.c}"` : "";
      h += `<td${cls}>${esc(cell.v || "")}</td>`;
    }
    h += `</tr>`;
  }
  h += `</tbody></table></div></div>`;
  return h;
}

function summaryCard(label, value, variant) {
  const cls = variant ? ` summary-item--${variant}` : "";
  return `<div class="summary-item${cls}"><div class="summary-label">${esc(label)}</div><div class="summary-value">${esc(value)}</div></div>`;
}

function esc(s) {
  const el = document.createElement("span");
  el.textContent = String(s);
  return el.innerHTML;
}

// ── Formatting helpers ──────────────────────────────────────
function formatAmount(n) {
  // Indian-style comma formatting: 1,23,456.78
  const parts = n.toFixed(2).split(".");
  let intPart = parts[0];
  const dec = parts[1];
  // Insert commas: last 3 digits, then groups of 2
  if (intPart.length > 3) {
    const last3 = intPart.slice(-3);
    let rest = intPart.slice(0, -3);
    const groups = [];
    while (rest.length > 2) {
      groups.unshift(rest.slice(-2));
      rest = rest.slice(0, -2);
    }
    if (rest.length > 0) groups.unshift(rest);
    intPart = groups.join(",") + "," + last3;
  }
  return intPart + "." + dec;
}

function formatNumber(n) {
  return n.toLocaleString("en-IN");
}

function formatMonth(isoMonth) {
  // "2026-01" → "Jan 2026"
  const [y, m] = isoMonth.split("-");
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${months[parseInt(m, 10) - 1] || m} ${y}`;
}

function formatDate(isoStr) {
  if (!isoStr || isoStr === "unknown") return "-";
  // Parse YYYY-MM-DD directly to avoid UTC midnight → local date shift.
  const m = isoStr.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (m) {
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return `${parseInt(m[3], 10)} ${months[parseInt(m[2], 10) - 1] || m[2]} ${m[1]}`;
  }
  return isoStr.slice(0, 10);
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

// ── Export: JSON ─────────────────────────────────────────────
function exportJSON() {
  if (!lastResult) return;
  const name = pendingFile ? pendingFile.name.replace(/\.pdf$/i, ".json") : "statement.json";
  download(
    new Blob([JSON.stringify(lastResult, null, 2)], { type: "application/json" }),
    name,
  );
}

// ── Export: CSV ──────────────────────────────────────────────
function exportCSV() {
  if (!lastResult) return;
  const d = lastResult;
  const fields = [
    "bank", "file", "source", "transaction_type",
    "person", "card_number", "date", "time", "narration", "reward_points",
    "amount", "amount_numeric", "signed_amount", "spend_amount", "credit_amount",
  ];

  const rows = [];

  function addRow(source, txn) {
    const amount = parseFloat(String(txn.amount || "0").replace(/,/g, "")) || 0;
    const txnType = txn.transaction_type || "";
    const isCredit = source === "payments_refunds" || txnType === "credit";
    const signed = isCredit ? -amount : amount;
    const spend = source === "transactions" ? amount : 0;
    const credit = isCredit ? amount : 0;

    rows.push({
      bank: d.bank || "",
      file: d.file || "",
      source,
      transaction_type: txnType,
      person: txn.person || "",
      card_number: txn.card_number || "",
      date: txn.date || "",
      time: txn.time || "",
      narration: txn.narration || "",
      reward_points: txn.reward_points || "",
      amount: String(txn.amount || "0"),
      amount_numeric: amount.toFixed(2),
      signed_amount: signed.toFixed(2),
      spend_amount: spend.toFixed(2),
      credit_amount: credit.toFixed(2),
    });
  }

  (d.transactions || []).forEach((t) => addRow("transactions", t));
  (d.payments_refunds || []).forEach((t) => addRow("payments_refunds", t));

  let csv = fields.join(",") + "\n";
  for (const row of rows) {
    csv += fields.map((f) => csvCell(row[f])).join(",") + "\n";
  }

  const name = pendingFile ? pendingFile.name.replace(/\.pdf$/i, ".csv") : "statement.csv";
  download(new Blob([csv], { type: "text/csv" }), name);
}

function csvCell(val) {
  const s = String(val);
  if (/[",\n\r]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}

function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
