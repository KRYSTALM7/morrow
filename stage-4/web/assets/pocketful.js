(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const main = $("#main-content");
  const tokenKey = "pocketful.token";
  const state = { token: localStorage.getItem(tokenKey), me: null, currency: "EUR", minorUnits: 2, dashboard: {} };
  let homeReadSequence = 0;
  let requestReadSequence = 0;
  let authorizationReadSequence = 0;
  const requestPayOperations = new Map();
  // Mirrors UI_ROUTES in app.py.
  const uiRoutes = new Set(["/", "/requests", "/split", "/assistant", "/paypal/return", "/signup", "/login", "/authorizations", "/commitments", "/activity"]);

  class NetworkFailure extends Error {}

  function freshKey() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
      return globalThis.crypto.randomUUID();
    }
    const bytes = new Uint8Array(24);
    globalThis.crypto.getRandomValues(bytes);
    return Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  }

  function operationTracker() {
    let key = null;
    let bodyText = null;
    const tracker = {
      changed() { key = null; bodyText = null; },
      forBody(body) {
        const nextBody = JSON.stringify(body);
        if (key === null || nextBody !== bodyText) {
          key = freshKey();
          bodyText = nextBody;
        }
        return key;
      }
    };
    return tracker;
  }

  async function api(path, { method = "GET", body, key } = {}) {
    const headers = { Accept: "application/json" };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (key) headers["Idempotency-Key"] = key;
    let response;
    try {
      response = await fetch(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store"
      });
    } catch (_) {
      // The browser's own text ("Failed to fetch") means nothing to people. Still a
      // NetworkFailure, so a lost payment keeps its uncertain, retry-safe handling.
      throw new NetworkFailure("Couldn’t reach Morrow. Check your connection and try again.");
    }
    if (response.status === 204 && method !== "GET") {
      // Financial and authentication writes return receipts. A successful status
      // without one leaves the operation outcome unknown, so retain its retry key.
      throw new NetworkFailure("The response could not be confirmed");
    }
    let data = null;
    if (response.status !== 204) {
      try { data = await response.json(); }
      catch (_) {
        // A successful write without its receipt is an unknown outcome. Preserve
        // the operation key/body so the caller can safely retry the same request.
        if (response.ok) throw new NetworkFailure("The response could not be confirmed");
        data = null;
      }
    }
    return { response, data };
  }

  function messageOf(result, fallback) {
    return result && result.data && result.data.error && result.data.error.message
      ? result.data.error.message : fallback;
  }

  function requireOk(result, fallback) {
    if (!result.response.ok) {
      const error = new Error(messageOf(result, fallback));
      error.status = result.response.status;
      error.payload = result.data;
      throw error;
    }
    return result.data;
  }

  function isUncertain(error) {
    return error instanceof NetworkFailure || (error && error.status >= 500);
  }

  function parseMoney(text) {
    const raw = String(text).trim();
    if (!raw || !/^\d+(?:\.\d*)?$/.test(raw)) return null;
    const pieces = raw.split(".");
    const whole = pieces[0];
    const fraction = pieces[1] || "";
    if (fraction.length > state.minorUnits) return null;
    const scale = 10n ** BigInt(state.minorUnits);
    try {
      const amount = BigInt(whole) * scale + BigInt(fraction.padEnd(state.minorUnits, "0") || "0");
      if (amount < 1n || amount > 1000000000n) return null;
      return Number(amount);
    } catch (_) {
      return null;
    }
  }

  function parseNonNegativeMoney(text) {
    const raw = String(text).trim();
    if (!raw || !/^\d+(?:\.\d*)?$/.test(raw)) return null;
    const [whole, fraction = ""] = raw.split(".");
    if (fraction.length > state.minorUnits) return null;
    try {
      const amount = BigInt(whole) * (10n ** BigInt(state.minorUnits)) +
        BigInt(fraction.padEnd(state.minorUnits, "0") || "0");
      return amount <= 1000000000n ? Number(amount) : null;
    } catch (_) { return null; }
  }

  function formatMoney(value, currency = state.currency, units = state.minorUnits) {
    let minor;
    try { minor = BigInt(String(value)); }
    catch (_) { minor = 0n; }
    const negative = minor < 0n;
    if (negative) minor = -minor;
    const scale = 10n ** BigInt(units);
    const whole = (minor / scale).toString();
    if (units === 0) return `${negative ? "−" : ""}${whole} ${currency}`;
    const fraction = (minor % scale).toString().padStart(units, "0");
    return `${negative ? "−" : ""}${whole}.${fraction} ${currency}`;
  }

  function decimalInput(value) {
    let minor;
    try { minor = BigInt(String(value)); }
    catch (_) { return ""; }
    const scale = 10n ** BigInt(state.minorUnits);
    const whole = (minor / scale).toString();
    if (!state.minorUnits) return whole;
    return `${whole}.${(minor % scale).toString().padStart(state.minorUnits, "0")}`;
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function testNode(tag, testId, className, text) {
    const element = node(tag, className, text);
    element.dataset.testid = testId;
    return element;
  }

  const iconPaths = {
    overview: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    requests: '<path d="M20 7v5h-5"/><path d="M4 17v-5h5"/><path d="M5.6 9a7 7 0 0 1 11.5-2L20 12M4 12l2.9 5a7 7 0 0 0 11.5-2"/>',
    split: '<path d="M7 4v6a4 4 0 0 0 4 4h6"/><path d="m14 11 3 3-3 3"/><path d="M7 20v-2"/>',
    holds: '<path d="M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Z"/><path d="m9 12 2 2 4-4"/>',
    assistant: '<path d="M12 3a7 7 0 0 0-4 12.7V19h8v-3.3A7 7 0 0 0 12 3Z"/><path d="M9 22h6M9 19h6M12 7v2m-3 2h6"/>',
    wallet: '<rect x="3" y="5" width="18" height="15" rx="3"/><path d="M3 9h18M16 14h2"/><path d="M6 5V3h12v2"/>',
    calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18M8 14h.01M12 14h.01M16 14h.01"/>',
    people: '<path d="M16 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M10 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM20 8v6m3-3h-6"/>',
    activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    forecast: '<path d="M3 3v18h18M7 14l4-4 4 3 6-7"/><path d="M17 6h4v4"/>',
    pay: '<path d="M12 2v20M17 6.5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
    request: '<path d="M12 19V5m-6 6 6-6 6 6"/>',
    reserve: '<path d="M4 7h16M6 7V4h12v3M6 7v13h12V7M10 11h4"/>',
    refresh: '<path d="M20 7v5h-5"/><path d="M20 12a8 8 0 1 0 2 5"/>',
    expand: '<path d="M8 3H5a2 2 0 0 0-2 2v3m13-5h3a2 2 0 0 1 2 2v3M3 16v3a2 2 0 0 0 2 2h3m13-5v3a2 2 0 0 1-2 2h-3"/>',
    close: '<path d="m18 6-12 12M6 6l12 12"/>'
  };

  function icon(name, className = "ui-icon") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", className);
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = iconPaths[name] || iconPaths.wallet;
    return svg;
  }

  function clearMessage(container, testId) {
    const current = $(`[data-testid="${testId}"]`, container);
    if (current) current.remove();
  }

  function showMessage(container, testId, text, type = "error-message") {
    clearMessage(container, testId);
    const message = testNode("div", testId, type, text);
    message.setAttribute("role", type === "error-message" ? "alert" : "status");
    container.prepend(message);
    return message;
  }

  function field(labelText, id, type = "text", placeholder = "", testId = id) {
    const wrap = node("div", "field");
    const label = node("label", "", labelText);
    label.htmlFor = id;
    const input = document.createElement(type === "select" ? "select" : "input");
    input.id = id;
    input.dataset.testid = testId;
    if (type !== "select") {
      input.type = type;
      input.placeholder = placeholder;
      if (type === "text") input.autocomplete = "off";
    }
    wrap.append(label, input);
    return { wrap, input };
  }

  function addCurrentIdentity() {
    const host = $("#account-nav");
    host.replaceChildren();
    if (!state.me || !state.token) return;
    const identity = testNode("span", "current-user", "account-name", state.me.display_name);
    const handle = testNode("span", "current-handle", "visually-hidden", state.me.handle);
    const logout = testNode("button", "logout-button", "logout-button", "Log out");
    logout.type = "button";
    logout.addEventListener("click", async () => {
      logout.disabled = true;
      try { await api("/auth/logout", {method: "POST", body: {}}); } catch (_) { /* local sign-out still proceeds */ }
      finally {
        localStorage.removeItem(tokenKey);
        state.token = null;
        state.me = null;
        location.assign("/login");
      }
    });
    host.append(identity, handle, logout);
  }

  function setCurrentNavigation() {
    const path = location.pathname;
    for (const anchor of document.querySelectorAll(".main-nav a")) {
      if (anchor.getAttribute("href") === path || (path === "/" && anchor.getAttribute("href") === "/")) {
        anchor.setAttribute("aria-current", "page");
      } else {
        anchor.removeAttribute("aria-current");
      }
    }
  }

  function setupMobileNavigation() {
    const toggle = $("#menu-toggle"), close = $("#drawer-close"), overlay = $("#drawer-overlay"), sidebar = $("#app-sidebar");
    if (!toggle || !close || !overlay || !sidebar) return;
    const mobile = window.matchMedia("(max-width: 760px)");
    const labels = ["Overview", "Requests", "Split bills", "Holds", "Assistant & payments"];
    const names = ["overview", "requests", "split", "holds", "assistant"];
    for (const [index, anchor] of [...sidebar.querySelectorAll(".main-nav a")].entries()) {
      if (!labels[index]) continue;
      const label = document.createElement("span"); label.className = "nav-label"; label.textContent = labels[index];
      anchor.replaceChildren(icon(names[index]), label);
      anchor.setAttribute("aria-label", labels[index]);
      anchor.title = labels[index];
    }
    close.replaceChildren(icon("close"));
    const syncToggle = () => {
      const drawerOpen = document.body.classList.contains("nav-open");
      if (mobile.matches) {
        toggle.setAttribute("aria-expanded", String(drawerOpen));
        toggle.setAttribute("aria-label", drawerOpen ? "Close navigation menu" : "Open navigation menu");
        sidebar.inert = !drawerOpen;
      } else {
        const expanded = !document.body.classList.contains("sidebar-collapsed");
        toggle.setAttribute("aria-expanded", String(expanded));
        toggle.setAttribute("aria-label", expanded ? "Collapse navigation sidebar" : "Expand navigation sidebar");
        sidebar.inert = false;
      }
    };
    const setDrawerOpen = (open, restoreFocus = true) => {
      document.body.classList.toggle("nav-open", open && mobile.matches);
      syncToggle();
      if (mobile.matches && open) setTimeout(() => { if (document.body.classList.contains("nav-open")) close.focus(); }, 0);
      else if (mobile.matches && restoreFocus) toggle.focus();
    };
    toggle.addEventListener("click", () => {
      if (mobile.matches) setDrawerOpen(!document.body.classList.contains("nav-open"));
      else {
        document.body.classList.toggle("sidebar-collapsed");
        syncToggle();
      }
    });
    close.addEventListener("click", () => setDrawerOpen(false));
    overlay.addEventListener("click", () => setDrawerOpen(false));
    sidebar.addEventListener("click", event => { if (event.target.closest(".main-nav a")) setDrawerOpen(false, false); });
    document.addEventListener("keydown", event => {
      if (!document.body.classList.contains("nav-open")) return;
      if (event.key === "Escape") { event.preventDefault(); setDrawerOpen(false); return; }
      if (event.key === "Tab") {
        const focusable = [...sidebar.querySelectorAll("button:not([disabled]), a[href]")];
        if (event.shiftKey && document.activeElement === focusable[0]) { event.preventDefault(); focusable.at(-1).focus(); }
        else if (!event.shiftKey && document.activeElement === focusable.at(-1)) { event.preventDefault(); focusable[0].focus(); }
      }
    });
    mobile.addEventListener("change", () => {
      document.body.classList.remove("nav-open");
      syncToggle();
    });
    syncToggle();
  }

  function pageHeading(kicker, title, description) {
    const header = node("header", "page-heading");
    const copy = node("div");
    copy.append(node("p", "eyebrow", kicker), node("h1", "", title), node("p", "lede", description));
    header.append(copy);
    return header;
  }

  function emptyState(title, detail) {
    const wrap = node("div", "empty-state");
    wrap.append(node("span", "empty-icon", "✦"), node("strong", "", title), node("p", "", detail));
    return wrap;
  }

  function expandedSection(id, title, iconName) {
    const section = testNode("section", id, "wallet-detail-section");
    const heading = node("h3", "wallet-detail-heading");
    heading.append(icon(iconName), document.createTextNode(title));
    section.append(heading, testNode("div", `${id}-content`, "wallet-detail-content", "Loading…"));
    return section;
  }

  function buildExpandedWalletSections() {
    const grid = node("div", "wallet-detail-grid");
    grid.append(expandedSection("wallet-position", "Wallet & this month", "wallet"),
      expandedSection("wallet-holds", "Open holds", "holds"),
      expandedSection("wallet-commitments", "Commitments & recurring bills", "calendar"),
      expandedSection("wallet-shared-expenses", "Shared expenses & reimbursements", "people"),
      expandedSection("wallet-transactions", "Recent wallet transactions", "activity"),
      expandedSection("wallet-outlook", "Safe-to-spend & outlook", "forecast"));
    return grid;
  }

  function refreshExpandedWallet() {
    const wallet = $("[data-testid='wallet-card']");
    if (!wallet || !wallet.classList.contains("wallet-expanded")) return;
    const expandedSequence = (state.dashboard.expandedSequence || 0) + 1;
    state.dashboard.expandedSequence = expandedSequence;
    for (const [endpoint, key] of [["/authorizations?direction=outgoing&status=open&limit=200", "holds"], ["/shared-expenses?status=open&limit=200", "shared"]]) {
      void api(endpoint).then(result => {
        if (expandedSequence !== state.dashboard.expandedSequence || !wallet.classList.contains("wallet-expanded")) return;
        state.dashboard[key] = requireOk(result, `Could not load ${key === "holds" ? "open holds" : "shared expenses"}.`);
        delete state.dashboard[`${key}Error`];
        if (key === "holds") renderWalletRecordSummary();
        renderExpandedWallet();
      }).catch(error => {
        if (expandedSequence !== state.dashboard.expandedSequence) return;
        const label = key === "holds" ? "open holds" : "shared expenses";
        state.dashboard[`${key}Error`] = error.message ? `Could not load ${label}: ${error.message}` : `Could not load ${label}.`;
        if (key === "holds") renderWalletRecordSummary();
        renderExpandedWallet();
      });
    }
    renderExpandedWallet();
  }

  function renderExpandedWallet() {
    const root = $(`[data-testid='wallet-expanded-content']`);
    if (!root) return;
    const me = state.me || {};
    const safe = state.dashboard.safe;
    const position = $(`[data-testid='wallet-position-content']`);
    position.replaceChildren(outlookRow("Available wallet funds", formatMoney(me.available === undefined ? me.balance : me.available)),
      outlookRow("Total wallet balance", formatMoney(me.total === undefined ? me.balance : me.total)));
    const month = state.dashboard.month;
    if (month && month.complete) {
      let incoming = 0n, outgoing = 0n;
      for (const entry of month.entries || []) {
        const delta = BigInt(String(entry.delta));
        if (delta > 0n) incoming += delta; else outgoing -= delta;
      }
      position.append(outlookRow("Money received this month", formatMoney(incoming.toString())),
        outlookRow("Money spent this month", formatMoney(outgoing.toString())));
    } else position.append(node("p", state.dashboard.monthError ? "error-message" : "field-hint",
      state.dashboard.monthError || "Month-to-date wallet movement is unavailable; the ledger could not be summarized completely."));
    position.append(node("p", "field-hint", "Morrow shows this wallet only; external bank balances and cash are not included."));

    const holdsContent = $(`[data-testid='wallet-holds-content']`);
    holdsContent.replaceChildren();
    if (state.dashboard.holdsError) holdsContent.append(node("p", "error-message", state.dashboard.holdsError));
    else if (!state.dashboard.holds) holdsContent.append(node("p", "muted", "Loading open holds…"));
    else {
      const holds = (state.dashboard.holds.authorizations || []).filter(item => item.status === "open" && item.from_user_id === me.user_id);
      const countLabel = state.dashboard.holds.has_more ? "200+ open holds" : holds.length === 1 ? "1 open hold" : `${holds.length} open holds`;
      holdsContent.append(outlookRow(countLabel, formatMoney(me.held || 0)));
      if (!holds.length) holdsContent.append(node("p", "field-hint", "No active wallet reservations."));
      for (const hold of holds.slice(0, 4)) holdsContent.append(outlookRow(`${hold.to_handle ? `For ${hold.to_handle}` : "Reservation"}${hold.note ? ` · ${hold.note}` : ""}`,
        formatMoney(hold.remaining_amount === undefined ? hold.amount : hold.remaining_amount, hold.currency || state.currency)));
    }

    const commitmentContent = $(`[data-testid='wallet-commitments-content']`);
    commitmentContent.replaceChildren();
    if (Number.isInteger(safe && safe.unpaid_commitments_minor)) commitmentContent.append(outlookRow("Commitments due this month", formatMoney(safe.unpaid_commitments_minor)));
    const commitments = (state.dashboard.commitments || []).filter(item => item.status === "active" && item.next_due_date)
      .sort((a, b) => String(a.next_due_date).localeCompare(String(b.next_due_date)));
    if (!state.dashboard.commitments) commitmentContent.append(node("p", state.dashboard.commitmentsError ? "error-message" : "muted",
      state.dashboard.commitmentsError || "Commitment data is loading."));
    else if (!commitments.length) commitmentContent.append(node("p", "field-hint", "No upcoming recurring commitments."));
    else for (const item of commitments.slice(0, 5)) commitmentContent.append(outlookRow(`${item.title} · ${item.next_due_date}`,
      formatMoney(item.amount_minor, item.currency, state.minorUnits)));

    const sharedContent = $(`[data-testid='wallet-shared-expenses-content']`);
    sharedContent.replaceChildren();
    if (state.dashboard.sharedError) sharedContent.append(node("p", "error-message", state.dashboard.sharedError));
    else if (!state.dashboard.shared) sharedContent.append(node("p", "muted", "Loading shared expenses…"));
    else {
      const expenses = state.dashboard.shared.expenses || [];
      if (state.dashboard.shared.has_more) sharedContent.append(node("p", "field-hint", "There are more than 200 open expenses; the outstanding total is not summarized here."));
      else {
        const receivable = expenses.reduce((sum, item) => sum + BigInt(String(item.outstanding_receivable_minor || 0)), 0n);
        sharedContent.append(outlookRow("Reimbursements outstanding (receivable)", formatMoney(receivable.toString())));
      }
      sharedContent.append(node("p", "field-hint", "Receivables are not wallet cash and are excluded from available and Safe-to-Spend balances."));
      if (!expenses.length) sharedContent.append(node("p", "field-hint", "No open shared expenses."));
      for (const expense of expenses.slice(0, 4)) sharedContent.append(outlookRow(expense.description || "Shared expense",
        formatMoney(expense.outstanding_receivable_minor || 0, expense.currency || state.currency)));
    }
    const requests = state.dashboard.requests || [];
    if (!state.dashboard.requests) sharedContent.append(node("p", state.dashboard.requestsError ? "error-message" : "muted",
      state.dashboard.requestsError || "Open P2P request data is loading."));
    else if (state.dashboard.requestsComplete === false) sharedContent.append(node("p", "field-hint", "More than 2,000 open requests; owed and receivable totals are not summarized here."));
    else if (state.dashboard.requests) {
      const owed = requests.filter(item => item.status === "pending" && item.payer_id === me.user_id)
        .reduce((sum, item) => sum + BigInt(String(item.amount)), 0n);
      const toReceive = requests.filter(item => item.status === "pending" && item.requester_id === me.user_id)
        .reduce((sum, item) => sum + BigInt(String(item.amount)), 0n);
      sharedContent.append(outlookRow("Open P2P requests owed", formatMoney(owed.toString())),
        outlookRow("Open P2P requests to receive", formatMoney(toReceive.toString())));
    }

    const activityContent = $(`[data-testid='wallet-transactions-content']`);
    activityContent.replaceChildren();
    const activity = state.dashboard.activity;
    if (!activity) activityContent.append(node("p", state.dashboard.activityError ? "error-message" : "muted",
      state.dashboard.activityError || "Recent wallet activity is loading."));
    else if (!activity.length) activityContent.append(node("p", "field-hint", "No wallet transactions yet."));
    else for (const payment of activity.slice(0, 5)) {
      const received = payment.to_user_id === me.user_id;
      const peer = received ? payment.from_handle : payment.to_handle;
      const date = new Date(payment.created_at);
      const day = Number.isNaN(date.getTime()) ? "Date unavailable" : new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric"}).format(date);
      activityContent.append(outlookRow(`${received ? "Received from" : "Paid"} ${peer} · ${day}`,
        formatMoney(payment.amount, payment.currency || state.currency)));
    }

    const outlook = $(`[data-testid='wallet-outlook-content']`);
    outlook.replaceChildren();
    if (!safe) outlook.append(node("p", state.dashboard.safeError ? "error-message" : "muted",
      state.dashboard.safeError || "Financial outlook is loading."));
    else {
      outlook.append(node("span", safe.status === "available" ? "status-pill status-completed" : "status-pill status-unavailable",
        safe.status === "available" ? `${safe.confidence} confidence` : "Safe-to-Spend unavailable"));
      if (safe.status === "available") {
        outlook.append(outlookRow("Safe-to-Spend", formatMoney(safe.safe_to_spend_minor)),
          outlookRow("Remaining variable spend forecast", formatMoney(safe.forecast_remaining_variable_spend_minor)));
        if (safe.forecast_range_minor) outlook.append(outlookRow("Forecast range",
          `${formatMoney(safe.forecast_range_minor.low)} – ${formatMoney(safe.forecast_range_minor.high)}`));
      } else outlook.append(node("p", "field-hint", `No amount shown. ${formatForecastReasons(safe.unavailable_reasons || [])}`));
      outlook.append(node("p", "field-hint", `Calculated from Morrow wallet history only · ${safe.calculation_date}.`));
      const details = document.createElement("details"); details.className = "forecast-assumptions";
      details.append(node("summary", "", "Calculation assumptions"));
      const list = node("ul");
      for (const assumption of safe.assumptions || []) list.append(node("li", "", assumption));
      details.append(list); outlook.append(details);
    }
  }

  function loadingState(testId, text) {
    const indicator = testNode("div", testId, "loading-state", text);
    indicator.setAttribute("role", "status");
    indicator.hidden = true;
    return indicator;
  }

  // While a list loads, only the indicator shows: no empty state and no stale rows.
  function setListLoading(container, indicator, loading) {
    container.setAttribute("aria-busy", String(loading));
    indicator.hidden = !loading;
  }

  function panelHeading(title, detail) {
    const wrap = node("div", "panel-heading");
    const copy = node("div");
    copy.append(node("h2", "", title));
    if (detail) copy.append(node("p", "", detail));
    wrap.append(copy);
    return wrap;
  }

  function makeNavForm(panelTitle, detail, formId, submitId, submitText) {
    const panel = node("section", "panel");
    panel.append(panelHeading(panelTitle, detail));
    const form = document.createElement("form");
    form.id = formId;
    form.className = "form-grid";
    form.noValidate = true;
    const feedback = node("div", "notice-list");
    form.append(feedback);
    const button = testNode("button", submitId, "primary-button", submitText);
    button.type = "submit";
    form.append(button);
    panel.append(form);
    return { panel, form, feedback, button };
  }

  function addVisibilitySelect(wrap, id, labelText = "Who can see this?") {
    const holder = node("div", "field");
    const label = node("label", "", labelText);
    label.htmlFor = id;
    const select = testNode("select", id, "");
    select.id = id;
    for (const [value, title] of [["public", "Friends · public"], ["private", "Only the people involved"]]) {
      const option = node("option", "", title);
      option.value = value;
      select.append(option);
    }
    holder.append(label, select);
    wrap.insertBefore(holder, wrap.querySelector("button[type='submit']"));
    return select;
  }

  function buildHome() {
    if (!state.me) {
      // The signed-out landing page swaps the app navigation for sign-in links.
      document.body.classList.add("is-public");
      document.body.classList.add("is-landing");
      const login = testNode("a", "header-login", "quiet-button", "Log in"); login.href = "/login";
      const signup = testNode("a", "header-signup", "primary-button", "Create account"); signup.href = "/signup";
      $("#account-nav").replaceChildren(login, signup);
      main.replaceChildren($("#landing-template").content.cloneNode(true));
      setupLandingWalletStory();
      return;
    }
    document.body.classList.remove("is-public", "is-landing");
    const firstName = String(state.me.display_name || "there").trim().split(/\s+/)[0];
    const heading = pageHeading("Overview", `Good morning, ${firstName}.`, "Here's where your money stands today.");
    heading.classList.add("dashboard-heading");
    main.replaceChildren(heading);
    const dashboard = node("div", "dashboard-overview");
    const wallet = testNode("section", "wallet-card", "panel wallet-hero");
    wallet.setAttribute("aria-label", "Wallet balance");
    wallet.setAttribute("role", "group");
    wallet.tabIndex = 0;
    const walletTop = node("div", "activity-top");
    const walletCopy = node("div");
    walletCopy.append(node("p", "eyebrow", "Wallet overview"));
    const available = testNode("strong", "wallet-available", "", "—");
    available.setAttribute("data-amount", "0");
    available.setAttribute("aria-label", "Available balance");
    walletCopy.append(available, node("p", "currency-caption", "Available to spend"));
    const refresh = testNode("button", "wallet-refresh", "wallet-refresh", "Refresh");
    refresh.type = "button";
    refresh.prepend(icon("refresh"));
    const toggleWallet = testNode("button", "wallet-toggle", "wallet-toggle");
    toggleWallet.type = "button";
    toggleWallet.setAttribute("aria-expanded", "false");
    toggleWallet.setAttribute("aria-controls", "wallet-expanded-content");
    toggleWallet.setAttribute("aria-label", "View full financial picture");
    const toggleLabel = (desktop, mobile) => {
      const longLabel = node("span", "wallet-toggle-desktop", desktop);
      const shortLabel = node("span", "wallet-toggle-mobile", mobile);
      return [longLabel, shortLabel];
    };
    toggleWallet.append(icon("expand"), ...toggleLabel("View full financial picture", "View full picture"));
    const walletTools = node("div", "wallet-tools"); walletTools.append(refresh, toggleWallet);
    walletTop.append(walletCopy, walletTools);
    const breakdown = node("div", "wallet-breakdown");
    const totalStat = node("div", "wallet-stat");
    totalStat.append(node("span", "", "Total balance"));
    const total = testNode("strong", "wallet-balance", "", "—");
    total.dataset.amount = "0";
    totalStat.append(total);
    breakdown.append(totalStat);
    if (Number(state.me.held || 0) > 0) breakdown.append(createHeldStat(state.me.held));
    const safeSpend = node("div", "safe-spend-status");
    const safeBadge = testNode("strong", "safe-to-spend-status", "status-pill status-unavailable", "Unavailable");
    const safeValue = testNode("strong", "safe-to-spend-value", "safe-spend-value", "—");
    const safeExplanation = testNode("p", "safe-to-spend-explanation", "", "Waiting for forecast inputs.");
    safeSpend.append(node("span", "safe-spend-label", "Safe to spend"), safeBadge,
      safeValue, safeExplanation);
    const walletRecords = testNode("p", "wallet-records-summary", "wallet-records-summary", "Commitments due this month — · Open holds —");
    const expandedClip = node("div", "wallet-expanded-clip");
    const expanded = testNode("div", "wallet-expanded-content", "wallet-expanded-content");
    expanded.id = "wallet-expanded-content";
    expanded.setAttribute("aria-hidden", "true");
    expanded.inert = true;
    expanded.append(buildExpandedWalletSections());
    expandedClip.append(expanded);
    wallet.append(walletTop, breakdown, safeSpend, walletRecords, expandedClip);
    refresh.addEventListener("click", () => { void refreshHome(); });
    const setWalletExpanded = open => {
      wallet.classList.toggle("wallet-expanded", open);
      dashboard.classList.toggle("wallet-expanded", open);
      toggleWallet.setAttribute("aria-expanded", String(open));
      toggleWallet.setAttribute("aria-label", open ? "Collapse financial picture" : "View full financial picture");
      toggleWallet.replaceChildren(icon(open ? "close" : "expand"), ...toggleLabel(
        open ? "Collapse financial picture" : "View full financial picture", open ? "Collapse wallet" : "View full picture"));
      expanded.setAttribute("aria-hidden", String(!open));
      expanded.inert = !open;
      if (open) void refreshExpandedWallet();
    };
    toggleWallet.addEventListener("click", event => {
      event.stopPropagation();
      setWalletExpanded(toggleWallet.getAttribute("aria-expanded") !== "true");
    });
    wallet.addEventListener("click", event => {
      if (!wallet.classList.contains("wallet-expanded") && !event.target.closest("button, a")) setWalletExpanded(true);
    });
    wallet.addEventListener("keydown", event => {
      if (event.target !== wallet || !["Enter", " "].includes(event.key)) return;
      event.preventDefault(); setWalletExpanded(!wallet.classList.contains("wallet-expanded"));
    });
    const homeError = node("div", "notice-list"); homeError.id = "home-notices";
    const monthFlow = node("section", "dashboard-stat-grid");
    monthFlow.setAttribute("aria-label", "Wallet movement this month");
    monthFlow.append(createDashboardMetric("month-in", "Money in · month to date", "—", "Wallet credits, including P2P and refunds."),
      createDashboardMetric("month-out", "Money out · month to date", "—", "Wallet debits, including P2P payments."));
    const summaryGrid = node("div", "dashboard-summary-grid");
    const commitmentsSummary = node("section", "dashboard-summary");
    commitmentsSummary.dataset.testid = "commitments-overview";
    commitmentsSummary.append(node("h2", "", "Upcoming commitments"),
      testNode("div", "commitments-overview-content", "outlook-content", "Loading commitments…"));
    const commitmentLink = node("a", "text-link", "View commitments →"); commitmentLink.href = "/commitments";
    commitmentsSummary.append(commitmentLink);
    const shared = node("section", "dashboard-summary");
    shared.dataset.testid = "shared-expenses-overview";
    shared.append(node("h2", "", "Shared expenses & requests"));
    const sharedRows = testNode("div", "shared-request-summary", "shared-request-summary");
    sharedRows.setAttribute("aria-live", "polite");
    sharedRows.append(node("p", "muted", "Loading open requests…"));
    shared.append(sharedRows, node("p", "field-hint", "Reimbursements stay separate from wallet funds; see the full financial picture for details."));
    const sharedLink = node("a", "text-link", "Review requests & splits →"); sharedLink.href = "/requests";
    shared.append(sharedLink);
    summaryGrid.append(commitmentsSummary, shared);

    const quickActions = node("section", "dashboard-quick-actions");
    quickActions.append(node("h2", "", "Quick actions"));
    const quickLinks = node("div", "quick-action-links");
    for (const [label, action, note] of [["Pay", "pay-form", "Send a payment"], ["Request", "request-form", "Ask for money"], ["Reserve", "authorize-form", "Set money aside"]]) {
      const link = node("a", "quick-action-link");
      link.href = `/assistant#${action}`;
      link.setAttribute("aria-label", `${label}: ${note}`);
      const iconName = label === "Pay" ? "pay" : label === "Request" ? "request" : "reserve";
      link.append(icon(iconName), node("span", "quick-action-copy"));
      link.lastElementChild.append(node("strong", "", label), node("small", "", note));
      quickLinks.append(link);
    }
    quickActions.append(quickLinks);

    const lower = node("div", "dashboard-lower");
    const commitments = node("section", "dashboard-preview");
    commitments.id = "upcoming-commitments-preview";
    commitments.append(node("div", "preview-heading", "Upcoming commitments"));
    commitments.append(testNode("div", "commitments-preview-content", "preview-list", "Loading commitments…"));
    const commitmentsMore = node("a", "text-link", "All commitments →"); commitmentsMore.href = "/commitments";
    commitments.append(commitmentsMore);
    const activityPanel = node("section", "dashboard-preview");
    activityPanel.id = "recent-activity-preview";
    activityPanel.append(node("div", "preview-heading", "Recent activity"));
    const activityRegion = node("div", "activity-preview-list"); activityRegion.id = "activity-region";
    activityRegion.setAttribute("aria-live", "polite"); activityRegion.setAttribute("aria-busy", "true");
    const initialActivityLoading = loadingState("activity-loading", "Loading activity…"); initialActivityLoading.hidden = false;
    activityRegion.append(initialActivityLoading); activityPanel.append(activityRegion);
    const activityMore = node("a", "text-link", "All activity →"); activityMore.href = "/activity"; activityPanel.append(activityMore);
    const forecast = node("section", "dashboard-preview");
    forecast.id = "financial-outlook-preview";
    forecast.append(node("div", "preview-heading", "Financial outlook"));
    const outlook = testNode("div", "forecast-overview-content", "outlook-content", "Loading forecast data…");
    outlook.setAttribute("aria-live", "polite");
    forecast.append(outlook);
    const forecastMore = node("a", "text-link", "Full outlook & settings →"); forecastMore.href = "/assistant#financial-outlook";
    forecast.append(forecastMore);
    lower.append(commitments, activityPanel, forecast);
    dashboard.append(wallet, monthFlow, quickActions, summaryGrid, lower, homeError);
    main.append(dashboard);
    void refreshHome();
  }

  function buildMoneyActionForms() {
    const pay = makeNavForm("Send a payment", "A little note makes it feel personal.", "pay-form", "pay-submit", "Send payment");
    const payTo = field("Pay who?", "pay-handle", "text", "Their handle");
    payTo.input.autocapitalize = "none"; payTo.input.autocomplete = "off";
    const payAmount = field("Amount", "pay-amount", "text", "0.00"); payAmount.input.inputMode = "decimal";
    const payNote = field("What’s it for?", "pay-note", "text", "Add a note (optional)"); payNote.input.maxLength = 200;
    pay.form.insertBefore(payTo.wrap, pay.button);
    pay.form.insertBefore(payAmount.wrap, pay.button);
    pay.form.insertBefore(payNote.wrap, pay.button);
    addVisibilitySelect(pay.form, "pay-visibility");
    const payTracker = operationTracker();
    for (const input of [payTo.input, payAmount.input, payNote.input, $("[data-testid='pay-visibility']", pay.form)]) {
      input.addEventListener("input", () => { payTracker.changed(); clearMessage(pay.feedback, "pay-error"); clearMessage(pay.feedback, "pay-uncertain"); clearMessage(pay.feedback, "pay-form-success"); });
      input.addEventListener("change", () => { payTracker.changed(); clearMessage(pay.feedback, "pay-error"); clearMessage(pay.feedback, "pay-uncertain"); clearMessage(pay.feedback, "pay-form-success"); });
    }
    pay.form.addEventListener("submit", async event => {
      event.preventDefault();
      clearMessage(pay.feedback, "pay-error"); clearMessage(pay.feedback, "pay-uncertain");
      const amount = parseMoney(payAmount.input.value);
      if (amount === null) { showMessage(pay.feedback, "pay-error", `Enter an amount with no more than ${state.minorUnits} decimal places.`); return; }
      const body = { to_handle: payTo.input.value.trim(), amount, note: payNote.input.value, visibility: $("[data-testid='pay-visibility']", pay.form).value };
      const key = payTracker.forBody(body);
      await runMoneyAction(pay, async () => requireOk(await api("/payments", { method: "POST", body, key }), "Payment was not accepted."), "pay-error", "pay-uncertain", "Payment sent.");
    });

    const request = makeNavForm("Request money", "Ask someone to send you a specific amount.", "request-form", "request-submit", "Send request");
    const requestTo = field("Request from", "request-handle", "text", "Their handle");
    requestTo.input.autocapitalize = "none";
    const requestAmount = field("Amount", "request-amount", "text", "0.00"); requestAmount.input.inputMode = "decimal";
    const requestNote = field("Reason", "request-note", "text", "Add a note (optional)"); requestNote.input.maxLength = 200;
    request.form.insertBefore(requestTo.wrap, request.button); request.form.insertBefore(requestAmount.wrap, request.button); request.form.insertBefore(requestNote.wrap, request.button);
    const requestTracker = operationTracker();
    for (const input of [requestTo.input, requestAmount.input, requestNote.input]) input.addEventListener("input", () => { requestTracker.changed(); clearMessage(request.feedback, "request-error"); });
    request.form.addEventListener("submit", async event => {
      event.preventDefault(); clearMessage(request.feedback, "request-error");
      const amount = parseMoney(requestAmount.input.value);
      if (amount === null) { showMessage(request.feedback, "request-error", `Enter an amount with no more than ${state.minorUnits} decimal places.`); return; }
      const body = { payer_handle: requestTo.input.value.trim(), amount, note: requestNote.input.value };
      const key = requestTracker.forBody(body);
      const result = await runMoneyAction(request, async () => requireOk(await api("/requests", { method: "POST", body, key }), "Request was not sent."), "request-error", null, "Request sent.");
      if (result) { requestTo.input.value = ""; requestAmount.input.value = ""; requestNote.input.value = ""; requestTracker.changed(); }
    });

    const authorize = makeNavForm("Reserve for later", "Set money aside for someone to collect in one or more captures.", "authorize-form", "authorize-submit", "Create hold");
    const authTo = field("For who?", "authorize-handle", "text", "Their handle"); authTo.input.autocapitalize = "none";
    const authAmount = field("Amount to reserve", "authorize-amount", "text", "0.00"); authAmount.input.inputMode = "decimal";
    const authNote = field("What’s it for?", "authorize-note", "text", "Add a note (optional)"); authNote.input.maxLength = 200;
    authorize.form.insertBefore(authTo.wrap, authorize.button); authorize.form.insertBefore(authAmount.wrap, authorize.button); authorize.form.insertBefore(authNote.wrap, authorize.button);
    addVisibilitySelect(authorize.form, "authorize-visibility");
    const authorizeTracker = operationTracker();
    const authVisibility = $("[data-testid='authorize-visibility']", authorize.form);
    for (const input of [authTo.input, authAmount.input, authNote.input, authVisibility]) input.addEventListener("input", () => { authorizeTracker.changed(); clearMessage(authorize.feedback, "authorize-error"); });
    authorize.form.addEventListener("submit", async event => {
      event.preventDefault(); clearMessage(authorize.feedback, "authorize-error");
      const amount = parseMoney(authAmount.input.value);
      if (amount === null) { showMessage(authorize.feedback, "authorize-error", `Enter an amount with no more than ${state.minorUnits} decimal places.`); return; }
      const body = { to_handle: authTo.input.value.trim(), amount, note: authNote.input.value, visibility: authVisibility.value };
      const key = authorizeTracker.forBody(body);
      await runMoneyAction(authorize, async () => requireOk(await api("/authorizations", { method: "POST", body, key }), "Hold was not created."), "authorize-error", null, "Money reserved.");
    });
    return {pay: pay.panel, request: request.panel, reserve: authorize.panel};
  }

  function buildForecastSettingsForm() {
    const settingsForm = document.createElement("form");
    settingsForm.className = "form-grid forecast-settings-form";
    settingsForm.dataset.testid = "forecast-settings-form";
    const savingsField = field("Monthly savings target", "forecast-savings-target", "text", "0.00");
    savingsField.input.inputMode = "decimal";
    savingsField.input.setAttribute("aria-describedby", "forecast-savings-hint");
    const bufferField = field("Safety buffer", "forecast-safety-buffer", "text", "0.00");
    bufferField.input.inputMode = "decimal";
    const settingsHint = node("p", "field-hint", "These are reserved amounts, not transactions.");
    settingsHint.id = "forecast-savings-hint";
    bufferField.input.setAttribute("aria-describedby", "forecast-savings-hint");
    settingsForm.append(savingsField.wrap, bufferField.wrap, settingsHint);
    const settingsFeedback = node("div", "forecast-settings-feedback");
    const settingsSave = testNode("button", "forecast-settings-save", "secondary-button", "Save targets");
    settingsSave.type = "submit";
    settingsForm.append(settingsSave, settingsFeedback);
    const settingsTracker = operationTracker();
    for (const input of [savingsField.input, bufferField.input]) {
      input.addEventListener("input", () => {
        settingsTracker.changed();
        input.dataset.edited = "true";
      });
    }
    settingsForm.addEventListener("submit", async event => {
      event.preventDefault();
      clearMessage(settingsFeedback, "forecast-settings-error");
      clearMessage(settingsFeedback, "forecast-settings-success");
      const savings = parseNonNegativeMoney(savingsField.input.value);
      const buffer = parseNonNegativeMoney(bufferField.input.value);
      if (savings === null || buffer === null) {
        showMessage(settingsFeedback, "forecast-settings-error",
          `Enter non-negative amounts with no more than ${state.minorUnits} decimal places.`);
        return;
      }
      const body = {savings_target_minor: savings, safety_buffer_minor: buffer};
      settingsSave.disabled = true;
      try {
        const response = requireOk(await api("/forecast/settings", {
          method: "PATCH", body, key: settingsTracker.forBody(body)
        }), "Could not save your forecast targets.");
        savingsField.input.value = decimalInput(response.savings_target_minor);
        bufferField.input.value = decimalInput(response.safety_buffer_minor);
        delete savingsField.input.dataset.edited;
        delete bufferField.input.dataset.edited;
        showMessage(settingsFeedback, "forecast-settings-success", "Forecast targets saved.", "success-message");
        void refreshHome();
      } catch (error) {
        showMessage(settingsFeedback, "forecast-settings-error", error.message || "Could not save your forecast targets.");
      } finally { settingsSave.disabled = false; }
    });
    return settingsForm;
  }

  function setupLandingWalletStory() {
    const stage = $(".wallet-stage", main);
    const scenes = [...main.querySelectorAll("[data-wallet-scene]")];
    if (!stage || !scenes.length || typeof window.IntersectionObserver !== "function" || window.matchMedia("(max-width: 860px)").matches || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    document.documentElement.classList.add("landing-enhanced");
    const labels = ["Wallet overview", "Available funds", "Upcoming commitments", "Shared expenses", "Financial outlook"];
    const observer = new IntersectionObserver(() => {
      const marker = window.innerHeight * 0.48;
      let current = scenes[0], nearest = Infinity;
      for (const scene of scenes) {
        const bounds = scene.getBoundingClientRect();
        const center = bounds.top + bounds.height / 2;
        const distance = Math.abs(center - marker);
        if (distance < nearest) { current = scene; nearest = distance; }
      }
      const sceneNumber = Number(current.dataset.walletScene) || 0;
      stage.dataset.scene = String(sceneNumber);
      const label = $("[data-testid='wallet-scene-label']", stage);
      if (label) label.textContent = labels[sceneNumber] || labels[0];
    }, { rootMargin: "-42% 0px -42% 0px", threshold: 0 });
    scenes.forEach(scene => observer.observe(scene));
  }

  function createDashboardMetric(testId, title, value, note) {
    const card = testNode("article", `dashboard-${testId}`, "panel dashboard-metric");
    card.append(node("span", "metric-label", title),
      testNode("strong", `dashboard-${testId}-value`, "metric-value", value),
      node("span", "metric-note", note));
    return card;
  }

  function createHeldStat(value) {
    const heldStat = node("div", "wallet-stat");
    heldStat.dataset.testid = "wallet-held-wrap";
    heldStat.append(node("span", "", "On hold"));
    const held = testNode("strong", "wallet-held", "", formatMoney(value));
    held.dataset.amount = String(value);
    heldStat.append(held);
    return heldStat;
  }

  async function runMoneyAction(formView, action, errorId, uncertainId, successText) {
    formView.button.disabled = true;
    try {
      await action();
      if (errorId) clearMessage(formView.feedback, errorId);
      if (uncertainId) clearMessage(formView.feedback, uncertainId);
      showMessage(formView.feedback, `${formView.form.id}-success`, successText, "success-message");
      await refreshHome();
      return true;
    } catch (error) {
      clearMessage(formView.feedback, `${formView.form.id}-success`);
      if (isUncertain(error) && uncertainId) {
        showMessage(formView.feedback, uncertainId, "We couldn’t confirm this payment yet. Your details are saved here—retry the same payment to check safely.", "uncertain-message");
      } else if (errorId) {
        showMessage(formView.feedback, errorId, error.message || "This action could not be completed.");
      }
      if (formView.form.id === "pay-form" || formView.form.id === "authorize-form") await refreshHome();
      return false;
    } finally {
      formView.button.disabled = false;
    }
  }

  async function refreshHome() {
    const region = $("#activity-region");
    if (!region || !state.token) return;
    const sequence = ++homeReadSequence;
    const notices = $("#home-notices");
    clearMessage(notices, "home-error");
    region.setAttribute("aria-busy", "true");
    const outcomes = await Promise.allSettled([
      api("/me"), readPersonalActivity(state.me && state.me.user_id), readMonthFlow(),
      readOpenRequests(), api("/commitments?status=active&limit=50")
    ]);
    if (sequence !== homeReadSequence) return;

    const [meOutcome, activityOutcome, monthOutcome, requestsOutcome, commitmentsOutcome] = outcomes;
    state.dashboard.activity = activityOutcome.status === "fulfilled" ? activityOutcome.value : null;
    state.dashboard.activityError = activityOutcome.status === "rejected" ? activityOutcome.reason.message || "Could not load recent wallet activity." : "";
    state.dashboard.month = monthOutcome.status === "fulfilled" ? monthOutcome.value : null;
    state.dashboard.monthError = monthOutcome.status === "rejected" ? monthOutcome.reason.message || "Could not summarize wallet movement." : "";
    state.dashboard.requests = requestsOutcome.status === "fulfilled" ? requestsOutcome.value.requests || [] : null;
    state.dashboard.requestsComplete = requestsOutcome.status === "fulfilled" ? requestsOutcome.value.complete : null;
    state.dashboard.requestsError = requestsOutcome.status === "rejected" ? requestsOutcome.reason.message || "Could not load open requests." : "";
    state.dashboard.commitments = commitmentsOutcome.status === "fulfilled" && commitmentsOutcome.value.response.ok
      ? commitmentsOutcome.value.data.commitments || [] : null;
    state.dashboard.commitmentsError = commitmentsOutcome.status === "rejected" ? commitmentsOutcome.reason.message
      : commitmentsOutcome.status === "fulfilled" && !commitmentsOutcome.value.response.ok ? "Could not load recurring commitments." : "";
    let hasError = false;
    if (meOutcome.status === "fulfilled" && meOutcome.value.response.ok) {
      const me = meOutcome.value.data;
      state.me = me; state.currency = me.currency; state.minorUnits = me.minor_units;
      const available = $(`[data-testid='wallet-available']`);
      const total = $(`[data-testid='wallet-balance']`);
      let held = $(`[data-testid='wallet-held']`);
      if (available && total) {
        available.textContent = formatMoney(me.available === undefined ? me.balance : me.available);
        available.dataset.amount = String(me.available === undefined ? me.balance : me.available);
        total.textContent = formatMoney(me.total === undefined ? me.balance : me.total);
        total.dataset.amount = String(me.total === undefined ? me.balance : me.total);
        const heldWrap = $(`[data-testid='wallet-held-wrap']`);
        if (Number(me.held || 0) > 0) {
          if (!heldWrap) $(".wallet-breakdown").append(createHeldStat(me.held));
          else {
            held = $(`[data-testid='wallet-held']`, heldWrap);
            held.textContent = formatMoney(me.held);
            held.dataset.amount = String(me.held);
          }
        } else if (heldWrap) heldWrap.remove();
      }
      addCurrentIdentity();
    } else {
      hasError = true;
      const error = meOutcome.status === "rejected" ? meOutcome.reason : new Error(messageOf(meOutcome.value, "Could not load wallet balance."));
      showMessage(notices, "home-error", error.message || "Could not load wallet balance.");
    }

    if (activityOutcome.status === "fulfilled") {
      renderActivity(region, activityOutcome.value);
    } else {
      hasError = true;
      region.replaceChildren(node("p", "error-message", activityOutcome.reason.message || "Could not load recent transactions."));
      showMessage(notices, "home-error", activityOutcome.reason.message || "Could not load recent transactions.");
    }
    region.setAttribute("aria-busy", "false");

    if (monthOutcome.status === "fulfilled") renderMonthFlow(monthOutcome.value);
    else {
      hasError = true;
      renderMonthFlow(null);
      showMessage(notices, "home-error", monthOutcome.reason.message || "Could not load this month’s wallet activity.");
    }

    const requestsRegion = $(`[data-testid='shared-request-summary']`);
    if (requestsOutcome.status === "fulfilled" && state.me) {
      renderRequestSummary(requestsRegion, requestsOutcome.value.requests || [], state.me.user_id,
        requestsOutcome.value.complete);
    } else {
      hasError = true;
      const message = requestsOutcome.status === "rejected" ? requestsOutcome.reason.message : "Could not identify this wallet.";
      requestsRegion.replaceChildren(node("p", "error-message", message || "Could not load open requests."));
    }
    if (commitmentsOutcome.status === "fulfilled" && commitmentsOutcome.value.response.ok) {
      renderCommitmentsPreview(commitmentsOutcome.value.data.commitments || []);
    } else {
      const preview = $(`[data-testid='commitments-preview-content']`);
      if (preview) preview.replaceChildren(node("p", "muted", "Commitments could not be loaded."));
    }
    renderWalletRecordSummary();
    if (hasError && !$("[data-testid='home-error']", notices)) {
      showMessage(notices, "home-error", "Some dashboard information could not be refreshed. Retry from the wallet card.");
    }
    void api("/safe-to-spend").then(result => {
      if (sequence !== homeReadSequence) return;
      if (result.response.ok) { state.dashboard.safe = result.data; state.dashboard.safeError = ""; renderFinancialOutlook(result.data); renderWalletRecordSummary(); }
      else {
        const message = messageOf(result, "Could not calculate your forecast.");
        state.dashboard.safe = null;
        state.dashboard.safeError = message;
        renderFinancialOutlook(null, message);
      renderWalletRecordSummary();
        showMessage(notices, "home-error", message);
      }
    }).catch(error => {
      if (sequence !== homeReadSequence) return;
      state.dashboard.safe = null;
      state.dashboard.safeError = error.message || "Could not calculate your forecast.";
      renderFinancialOutlook(null, error.message || "Could not calculate your forecast.");
      renderWalletRecordSummary();
      showMessage(notices, "home-error", error.message || "Could not calculate your forecast.");
    });
    if ($("[data-testid='wallet-card']")?.classList.contains("wallet-expanded")) renderExpandedWallet();
  }

  function renderFinancialOutlook(result, failureMessage = "") {
    const badge = $(`[data-testid='safe-to-spend-status']`);
    const value = $(`[data-testid='safe-to-spend-value']`);
    const explanation = $(`[data-testid='safe-to-spend-explanation']`);
    const commitmentRegion = $(`[data-testid='commitments-overview-content']`);
    const forecastRegion = $(`[data-testid='forecast-overview-content']`);
    if (!badge || !value || !explanation || !commitmentRegion || !forecastRegion) return;
    if (!result) {
      badge.className = "status-pill status-unavailable";
      badge.textContent = "Unavailable";
      value.textContent = "Unavailable";
      value.hidden = true;
      explanation.textContent = failureMessage || "Forecast inputs could not be loaded.";
      forecastRegion.replaceChildren(node("p", "error-message", failureMessage || "Forecast inputs could not be loaded."));
      if ($("[data-testid='wallet-card']")?.classList.contains("wallet-expanded")) renderExpandedWallet();
      return;
    }

    const settings = [
      [$(`[data-testid='forecast-savings-target']`), result.savings_target_minor],
      [$(`[data-testid='forecast-safety-buffer']`), result.safety_buffer_minor]
    ];
    for (const [input, amount] of settings) {
      if (input && !input.dataset.edited && Number.isInteger(amount)) input.value = decimalInput(amount);
    }

    const available = result.status === "available";
    badge.className = available ? "status-pill status-completed" : "status-pill status-unavailable";
    badge.textContent = available ? `${result.confidence} confidence` : "Unavailable";
    value.textContent = available ? formatMoney(result.safe_to_spend_minor) : "Unavailable";
    value.hidden = !available;
    const historyMonths = result.data_coverage && result.data_coverage.history_months || 0;
    explanation.textContent = available
      ? `Calculated ${result.calculation_date} · ${historyMonths} complete history months · available funds already reflect holds.`
      : `No safe-to-spend amount shown. ${formatForecastReasons(result.unavailable_reasons || [])}`;

    forecastRegion.replaceChildren();
    if (!available) {
      forecastRegion.append(node("span", "status-pill status-unavailable", "History coverage incomplete"),
        node("p", "", formatForecastReasons(result.unavailable_reasons || [])));
    } else {
      const range = result.forecast_range_minor;
      forecastRegion.append(outlookRow("Remaining variable spend",
        formatMoney(result.forecast_remaining_variable_spend_minor)));
      forecastRegion.append(outlookRow("Forecast range", `${formatMoney(range.low)} – ${formatMoney(range.high)}`));
    }
    forecastRegion.append(node("p", "field-hint", `Updated ${result.calculation_date}.`));
    if ($("[data-testid='wallet-card']")?.classList.contains("wallet-expanded")) renderExpandedWallet();
  }

  function renderWalletRecordSummary() {
    const summary = $(`[data-testid='wallet-records-summary']`);
    if (!summary) return;
    const unpaid = state.dashboard.safe && Number.isInteger(state.dashboard.safe.unpaid_commitments_minor)
      ? formatMoney(state.dashboard.safe.unpaid_commitments_minor) : "Unavailable";
    const holds = state.dashboard.holds && state.dashboard.holds.authorizations
      ? (state.dashboard.holds.authorizations.filter(item => item.status === "open" && item.from_user_id === (state.me && state.me.user_id)).length)
      : null;
    const held = state.me ? formatMoney(state.me.held || 0) : "Unavailable";
    summary.textContent = `Commitments due this month ${unpaid} · Open holds ${holds === null ? `${held} reserved` : `${holds} (${held})`}`;
  }

  function renderCommitmentsPreview(items) {
    const summary = $(`[data-testid='commitments-overview-content']`);
    const preview = $(`[data-testid='commitments-preview-content']`);
    const active = items.filter(item => item.status === "active");
    const upcoming = active.filter(item => item.next_due_date)
      .sort((a, b) => String(a.next_due_date).localeCompare(String(b.next_due_date)));
    if (summary) summary.textContent = active.length === 1 ? "1 active commitment" : `${active.length} active commitments`;
    if (!preview) return;
    preview.replaceChildren();
    if (!upcoming.length) {
      preview.append(emptyState("No upcoming commitments", "Add or review recurring bills when you’re ready."));
      return;
    }
    for (const item of upcoming.slice(0, 3)) {
      const row = node("div", "commitment-preview-row");
      row.append(node("span", "", item.title), node("time", "", item.next_due_date),
        node("strong", "", formatMoney(item.amount_minor, item.currency, state.minorUnits)));
      preview.append(row);
    }
  }

  function renderFullOutlook(container, result) {
    container.replaceChildren();
    const available = result.status === "available";
    container.append(node("span", available ? "status-pill status-completed" : "status-pill status-unavailable",
      available ? `${result.confidence} confidence` : "Safe-to-Spend unavailable"));
    if (!available) {
      container.append(node("p", "", `No safe-to-spend amount shown. ${formatForecastReasons(result.unavailable_reasons || [])}`));
    } else {
      container.append(outlookRow("Safe-to-Spend", formatMoney(result.safe_to_spend_minor)));
      container.append(outlookRow("Commitments due this month", formatMoney(result.unpaid_commitments_minor)));
      container.append(outlookRow("Remaining variable spend", formatMoney(result.forecast_remaining_variable_spend_minor)));
      const range = result.forecast_range_minor;
      container.append(outlookRow("Forecast range", `${formatMoney(range.low)} – ${formatMoney(range.high)}`));
    }
    container.append(node("p", "field-hint", `Based on Morrow wallet history; updated ${result.calculation_date}.`));
    const assumptions = document.createElement("details"); assumptions.className = "forecast-assumptions";
    assumptions.append(node("summary", "", "How this estimate is calculated"));
    const list = node("ul");
    for (const note of result.assumptions || []) list.append(node("li", "", note));
    if (result.data_coverage && result.data_coverage.variable_category_reason) list.append(node("li", "", result.data_coverage.variable_category_reason));
    assumptions.append(list); container.append(assumptions);
  }

  function outlookRow(label, amount) {
    const row = node("div", "outlook-row");
    row.append(node("span", "", label), node("strong", "", amount));
    return row;
  }

  function formatForecastReasons(reasons) {
    const labels = {
      insufficient_complete_history: "At least three complete months of wallet history are required.",
      statement_history_incomplete: "The statement history is incomplete.",
      wallet_history_coverage_unknown: "Wallet history coverage is unknown.",
      wallet_history_coverage_invalid: "Wallet history coverage is invalid.",
      commitment_currency_mismatch: "Commitments use a different currency.",
      commitment_occurrence_invalid: "A commitment occurrence could not be verified.",
      statement_entry_invalid: "A statement entry could not be verified."
    };
    return reasons.map(reason => labels[reason] || "A required financial input is unavailable.").join(" ");
  }

  async function readPersonalActivity(userId) {
    if (!userId) throw new Error("Could not identify this wallet.");
    const matches = [];
    const first = requireOk(await api("/activity"), "Could not load recent transactions.");
    let result = first;
    let offset = 50;
    const addPersonal = payments => {
      for (const payment of payments || []) {
        if (payment.from_user_id === userId || payment.to_user_id === userId) matches.push(payment);
      }
    };
    addPersonal(first.payments || []);
    while (result.has_more && offset < 1000 && matches.length < 8) {
      result = requireOk(await api(`/activity?limit=100&offset=${offset}`), "Could not load recent transactions.");
      addPersonal(result.payments || []);
      offset += 100;
    }
    return matches.slice(0, 8);
  }

  async function readMonthFlow() {
    const now = new Date();
    const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1));
    const params = new URLSearchParams({from: start.toISOString(), to: now.toISOString(), limit: "200"});
    let page = requireOk(await api(`/statement?${params}`), "Could not load this month’s wallet activity.");
    const entries = [...(page.entries || [])];
    let offset = entries.length;
    let pageCount = 1;
    while (page.has_more && pageCount < 10) {
      const nextParams = new URLSearchParams({snapshot: page.snapshot, limit: "200", offset: String(offset)});
      page = requireOk(await api(`/statement?${nextParams}`), "Could not load this month’s wallet activity.");
      entries.push(...(page.entries || []));
      offset += (page.entries || []).length;
      pageCount += 1;
    }
    return {entries, complete: !page.has_more};
  }

  async function readOpenRequests() {
    const requests = [];
    let page = requireOk(await api("/requests?status=pending&limit=200"), "Could not load open requests.");
    requests.push(...(page.requests || []));
    let offset = requests.length;
    let pageCount = 1;
    while (page.has_more && pageCount < 10) {
      page = requireOk(await api(`/requests?status=pending&limit=200&offset=${offset}`), "Could not load open requests.");
      requests.push(...(page.requests || []));
      offset += (page.requests || []).length;
      pageCount += 1;
    }
    return {requests, complete: !page.has_more};
  }

  function renderMonthFlow(result) {
    const incoming = $(`[data-testid='dashboard-month-in-value']`);
    const outgoing = $(`[data-testid='dashboard-month-out-value']`);
    if (!incoming || !outgoing) return;
    if (!result || !result.complete) {
      incoming.textContent = outgoing.textContent = "—";
      const note = result ? "More than 2,000 ledger entries; total not summarized." : "Could not load this wallet total.";
      $(`[data-testid='dashboard-month-in'] .metric-note`).textContent = note;
      $(`[data-testid='dashboard-month-out'] .metric-note`).textContent = note;
      return;
    }
    let moneyIn = 0n, moneyOut = 0n;
    for (const entry of result.entries) {
      const delta = BigInt(String(entry.delta));
      if (delta > 0n) moneyIn += delta;
      else moneyOut -= delta;
    }
    incoming.textContent = formatMoney(moneyIn.toString());
    outgoing.textContent = formatMoney(moneyOut.toString());
  }

  function renderRequestSummary(region, requests, userId, complete = true) {
    region.replaceChildren();
    if (!complete) {
      region.append(node("p", "shared-request-empty", "More than 2,000 open requests. Totals aren’t summarized here."));
      return;
    }
    const pending = requests.filter(request => request.status === "pending");
    if (!pending.length) {
      region.append(node("p", "shared-request-empty", "No open payment requests."));
      return;
    }
    const totals = new Map();
    for (const request of pending) {
      const incoming = request.payer_id === userId;
      const handle = incoming ? request.requester_handle : request.payer_handle;
      const key = `${incoming ? "from" : "to"}:${handle}`;
      const entry = totals.get(key) || {incoming, handle, amount: 0n};
      entry.amount += BigInt(String(request.amount));
      totals.set(key, entry);
    }
    for (const summary of totals.values()) {
      const row = node("div", "shared-request-row");
      row.append(node("span", "", summary.incoming ? `Requested from you · ${summary.handle}` : `You requested · ${summary.handle}`),
        node("strong", "", formatMoney(summary.amount.toString())));
      region.append(row);
    }
  }

  function renderActivity(region, payments) {
    region.replaceChildren();
    region.setAttribute("aria-busy", "false");
    if (!payments.length) {
      const empty = emptyState("No wallet transactions yet", "Payments you send or receive will appear here.");
      empty.dataset.testid = "empty-activity";
      region.append(empty);
      return;
    }
    const list = testNode("div", "activity-list", "activity-list");
    for (const payment of payments) {
      const item = testNode("article", `activity-item-${payment.payment_id}`, "activity-item");
      item.dataset.visibility = payment.visibility;
      const top = node("div", "activity-top");
      const received = payment.to_user_id === state.me.user_id;
      item.dataset.flow = received ? "in" : "out";
      const parties = testNode("div", `activity-parties-${payment.payment_id}`, "activity-parties", `${payment.from_handle} paid ${payment.to_handle}`);
      const amount = testNode("strong", `activity-amount-${payment.payment_id}`, "activity-amount", formatMoney(payment.amount, payment.currency || state.currency));
      top.append(parties, amount);
      const note = testNode("p", `activity-note-${payment.payment_id}`, "activity-note", payment.note || "");
      const meta = node("div", "row-meta");
      const privacy = node("span", `privacy-pill ${payment.visibility === "private" ? "private" : ""}`, payment.visibility === "private" ? "Private" : "Friends");
      let dateLabel = "Date unavailable";
      const createdAt = new Date(payment.created_at);
      if (!Number.isNaN(createdAt.getTime())) dateLabel = new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric"}).format(createdAt);
      meta.append(privacy, node("span", "activity-date", dateLabel));
      item.append(top, note, meta); list.append(item);
    }
    region.append(list);
  }

  function buildRequests() {
    main.replaceChildren(pageHeading("Between people", "Requests", "Keep track of what’s coming in and what you’ve asked for."));
    const panel = node("section", "panel");
    panel.append(panelHeading("Your requests", "Pay, decline, or cancel while a request is still pending."));
    const feedback = node("div", "notice-list"); feedback.id = "request-notices"; panel.append(feedback);
    const empty = emptyState("All caught up", "Requests you send or receive will appear here."); empty.dataset.testid = "empty-requests";
    empty.hidden = true;
    const loading = loadingState("requests-loading", "Loading requests…");
    const columns = testNode("div", "request-lists", "two-fields"); columns.classList.add("request-columns");
    const incomingSection = node("section", ""); incomingSection.append(node("h3", "", "Incoming"));
    const incoming = testNode("div", "incoming-list", "request-list");
    const outgoingSection = node("section", ""); outgoingSection.append(node("h3", "", "Outgoing"));
    const outgoing = testNode("div", "outgoing-list", "request-list");
    incomingSection.append(incoming); outgoingSection.append(outgoing); columns.append(incomingSection, outgoingSection);
    panel.append(loading, empty, columns); main.append(panel);
    void refreshRequests();
  }

  async function refreshRequests() {
    const incoming = $("[data-testid='incoming-list']");
    const outgoing = $("[data-testid='outgoing-list']");
    if (!incoming || !outgoing || !state.token) return;
    const sequence = ++requestReadSequence;
    const columns = $("[data-testid='request-lists']");
    const loading = $("[data-testid='requests-loading']");
    const empty = $("[data-testid='empty-requests']");
    empty.hidden = true; incoming.hidden = true; outgoing.hidden = true;
    setListLoading(columns, loading, true);
    let result;
    try { result = await api("/requests?limit=200"); }
    catch (error) {
      if (sequence !== requestReadSequence) return;
      setListLoading(columns, loading, false);
      showMessage($("#request-notices"), "request-error", error.message || "Could not load requests.");
      return;
    }
    if (sequence !== requestReadSequence) return;
    setListLoading(columns, loading, false);
    if (!result.response.ok) { showMessage($("#request-notices"), "request-error", messageOf(result, "Could not load requests.")); return; }
    incoming.replaceChildren(); outgoing.replaceChildren();
    const requests = result.data.requests || [];
    for (const request of requests) {
      const isIncoming = request.payer_id === state.me.user_id;
      const target = isIncoming ? incoming : outgoing;
      const row = testNode("article", `request-item-${request.request_id}`, "request-item");
      row.dataset.status = request.status;
      const top = node("div", "row-top");
      const copy = node("div");
      const other = isIncoming ? request.requester_handle : request.payer_handle;
      copy.append(node("div", "row-title", isIncoming ? `${other} asked you` : `You asked ${other}`));
      const status = node("span", `status-pill ${request.status}`, request.status);
      const amount = testNode("strong", `request-amount-${request.request_id}`, "row-amount", formatMoney(request.amount, request.currency || state.currency));
      top.append(copy, amount); row.append(top);
      const meta = node("div", "row-meta"); meta.append(status); row.append(meta);
      const note = node("p", "activity-note", request.note || ""); row.append(note);
      if (request.status === "pending") {
        const actions = node("div", "row-actions");
        if (isIncoming) {
          const pay = testNode("button", `request-pay-${request.request_id}`, "primary-button", "Pay request"); pay.type = "button";
          const decline = testNode("button", `request-decline-${request.request_id}`, "quiet-button", "Decline"); decline.type = "button";
          pay.addEventListener("click", () => handleRequestAction(request, "pay", pay));
          decline.addEventListener("click", () => handleRequestAction(request, "decline", decline));
          actions.append(pay, decline);
        } else {
          const cancel = testNode("button", `request-cancel-${request.request_id}`, "quiet-button", "Cancel request"); cancel.type = "button";
          cancel.addEventListener("click", () => handleRequestAction(request, "cancel", cancel));
          actions.append(cancel);
        }
        row.append(actions);
      }
      target.append(row);
    }
    const noRequests = requests.length === 0;
    empty.hidden = !noRequests;
    if (noRequests) { incoming.hidden = true; outgoing.hidden = true; }
    else { incoming.hidden = false; outgoing.hidden = false; }
  }

  async function handleRequestAction(request, action, button) {
    button.disabled = true;
    clearMessage($("#request-notices"), "request-error");
    try {
      let path = `/requests/${encodeURIComponent(request.request_id)}/${action}`;
      let result;
      if (action === "pay") {
        let operation = requestPayOperations.get(request.request_id);
        if (!operation) { operation = { key: freshKey(), bodyText: "{}" }; requestPayOperations.set(request.request_id, operation); }
        result = await api(path, { method: "POST", body: {}, key: operation.key });
      } else {
        result = await api(path, { method: "POST", body: {} });
      }
      requireOk(result, "This request could not be updated.");
      await refreshRequests();
      await refreshHomeIfMounted();
    } catch (error) {
      showMessage($("#request-notices"), "request-error", error.message || "This request could not be updated.");
      await refreshRequests();
      await refreshHomeIfMounted();
    } finally {
      button.disabled = false;
    }
  }

  async function refreshHomeIfMounted() {
    if ($("#activity-region")) await refreshHome();
  }

  function buildSplit() {
    main.replaceChildren(pageHeading("Make it even", "Split a shared cost", "Divide the amount in minor units. The first handles in your list receive any remainder."));
    const container = node("div", "split-form-container");
    const panel = node("section", "panel");
    panel.append(panelHeading("Create a split", "Preview the exact shares before you send requests."));
    const form = document.createElement("form"); form.className = "form-grid"; form.noValidate = true;
    const feedback = node("div", "notice-list");
    const amount = field("Total amount", "split-amount", "text", "0.00"); amount.input.inputMode = "decimal";
    const handles = field("Participants, in order", "split-handles", "text", "alex, sam, you");
    handles.input.autocapitalize = "none";
    const note = field("Note", "split-note", "text", "What did you share? (optional)"); note.input.maxLength = 200;
    const previewRegion = node("div"); previewRegion.id = "split-preview-region";
    const submit = testNode("button", "split-submit", "primary-button", "Create requests"); submit.type = "submit";
    form.append(feedback, amount.wrap, handles.wrap, note.wrap, previewRegion, submit);
    panel.append(form); container.append(panel); main.append(container);
    const tracker = operationTracker();
    for (const input of [amount.input, handles.input, note.input]) input.addEventListener("input", () => { tracker.changed(); clearMessage(feedback, "split-error"); refreshSplitPreview(previewRegion, amount.input.value, handles.input.value); });
    form.addEventListener("submit", async event => {
      event.preventDefault(); clearMessage(feedback, "split-error");
      const total = parseMoney(amount.input.value);
      const participants = handles.input.value.split(",").map(value => value.trim());
      if (total === null || !participants.length || participants.some(value => !value) || new Set(participants).size !== participants.length) {
        showMessage(feedback, "split-error", "Check the amount and enter each participant handle once."); return;
      }
      const body = { amount: total, participant_handles: participants, note: note.input.value };
      const key = tracker.forBody(body);
      submit.disabled = true;
      try {
        requireOk(await api("/splits", { method: "POST", body, key }), "Split could not be created.");
        showMessage(feedback, "split-success", "Requests are on their way.", "success-message");
        await refreshSplitPreview(previewRegion, amount.input.value, handles.input.value);
      } catch (error) {
        showMessage(feedback, "split-error", error.message || "Split could not be created.");
      } finally { submit.disabled = false; }
    });
  }

  function refreshSplitPreview(region, rawAmount, rawHandles) {
    region.replaceChildren();
    const amount = parseMoney(rawAmount);
    const handles = rawHandles.split(",").map(value => value.trim());
    if (amount === null || !handles.length || handles.some(value => !value) || new Set(handles).size !== handles.length) return;
    const preview = testNode("section", "split-preview", "split-preview");
    preview.append(node("div", "split-preview-title", "Your share preview"));
    const count = BigInt(handles.length);
    const total = BigInt(amount);
    const base = total / count;
    const remainder = total % count;
    handles.forEach((handle, index) => {
      const row = node("div", "split-share-row");
      row.append(node("span", "", handle));
      const share = testNode("strong", `split-share-${handle}`, "", formatMoney(base + (BigInt(index) < remainder ? 1n : 0n)));
      row.append(share); preview.append(row);
    });
    region.append(preview);
  }

  function buildAuthorizations() {
    main.replaceChildren(pageHeading("Money set aside", "Authorizations", "See what’s reserved, capture money sent to you, or release a hold you created."));
    const panel = node("section", "panel");
    panel.append(panelHeading("Your holds", "Open holds reduce what’s available to spend. Captures move money."));
    const feedback = node("div", "notice-list"); feedback.id = "authorization-notices"; panel.append(feedback);
    const empty = emptyState("No holds yet", "Money you reserve or authorize for you to collect will show up here."); empty.dataset.testid = "empty-authorizations";
    empty.hidden = true;
    const loading = loadingState("authorizations-loading", "Loading holds…");
    const list = testNode("div", "authorization-list", "authorization-list");
    panel.append(loading, empty, list); main.append(panel);
    void refreshAuthorizations();
  }

  async function refreshAuthorizations() {
    const list = $("[data-testid='authorization-list']");
    if (!list || !state.token) return;
    const sequence = ++authorizationReadSequence;
    const loading = $("[data-testid='authorizations-loading']");
    const empty = $("[data-testid='empty-authorizations']");
    empty.hidden = true; list.hidden = true;
    setListLoading(list, loading, true);
    let result;
    try { result = await api("/authorizations?limit=200"); }
    catch (error) {
      if (sequence !== authorizationReadSequence) return;
      setListLoading(list, loading, false);
      showMessage($("#authorization-notices"), "authorization-error", error.message || "Could not load authorizations.");
      return;
    }
    if (sequence !== authorizationReadSequence) return;
    setListLoading(list, loading, false);
    if (!result.response.ok) { showMessage($("#authorization-notices"), "authorization-error", messageOf(result, "Could not load authorizations.")); return; }
    const authorizations = result.data.authorizations || [];
    list.replaceChildren();
    for (const authorization of authorizations) list.append(renderAuthorization(authorization));
    list.hidden = false;
    empty.hidden = authorizations.length > 0;
  }

  function renderAuthorization(authorization) {
    const id = authorization.authorization_id;
    const incoming = authorization.to_user_id === state.me.user_id;
    const row = testNode("article", `authorization-item-${id}`, "authorization-item");
    row.dataset.status = authorization.status;
    const top = node("div", "row-top");
    const copy = node("div");
    const person = incoming ? authorization.from_handle : authorization.to_handle;
    copy.append(node("div", "row-title", incoming ? `Reserved by ${person}` : `Reserved for ${person}`));
    const amount = testNode("strong", `authorization-amount-${id}`, "row-amount", formatMoney(authorization.amount, authorization.currency || state.currency));
    top.append(copy, amount); row.append(top);
    const status = node("span", `status-pill ${authorization.status}`, authorization.status);
    const meta = node("div", "row-meta"); meta.append(status); row.append(meta);
    const note = node("p", "activity-note", authorization.note || ""); row.append(note);
    const info = node("div", "authorization-info");
    const expiry = testNode("time", `authorization-expires-${id}`, "", authorization.expires_at);
    expiry.dateTime = authorization.expires_at;
    // The time element's text must stay the exact RFC 3339 value, so the label sits beside it.
    const expiryLine = node("span");
    expiryLine.append("Expires ", expiry);
    info.append(expiryLine);
    if (authorization.status === "captured") {
      info.append(testNode("span", `authorization-captured-${id}`, "", formatMoney(authorization.captured_amount, authorization.currency || state.currency)));
    } else if (authorization.status === "open" && Number(authorization.captured_amount || 0) > 0) {
      info.append(node("span", "", `${formatMoney(authorization.captured_amount, authorization.currency || state.currency)} captured so far`));
    }
    row.append(info);
    if (authorization.status === "open" && incoming) {
      const actions = node("div", "row-actions");
      const captureInput = field("Capture amount", `capture-input-${id}`, "text", "");
      captureInput.input.inputMode = "decimal";
      captureInput.input.dataset.testid = `authorization-capture-amount-${id}`;
      captureInput.input.value = decimalInput(authorization.remaining_amount === undefined ? authorization.amount - (authorization.captured_amount || 0) : authorization.remaining_amount);
      captureInput.wrap.classList.add("authorization-capture-field");
      const finalOption = node("label", "capture-final-option");
      const finalInput = document.createElement("input");
      finalInput.type = "checkbox";
      finalInput.checked = true;
      finalInput.dataset.testid = `authorization-capture-final-${id}`;
      const finalText = node("span", "", "Release the remaining hold after capture");
      finalOption.append(finalInput, finalText);
      const capture = testNode("button", `authorization-capture-${id}`, "primary-button", "Capture funds"); capture.type = "button";
      const tracker = operationTracker();
      captureInput.input.addEventListener("input", () => tracker.changed());
      finalInput.addEventListener("change", () => tracker.changed());
      capture.addEventListener("click", async () => {
        const amountMinor = parseMoney(captureInput.input.value);
        if (amountMinor === null) { showMessage($("#authorization-notices"), "authorization-error", `Enter a valid amount with up to ${state.minorUnits} decimal places.`); return; }
        clearMessage($("#authorization-notices"), "authorization-error");
        const body = { amount: amountMinor, final: finalInput.checked };
        const key = tracker.forBody(body);
        capture.disabled = true;
        try { requireOk(await api(`/authorizations/${encodeURIComponent(id)}/capture`, { method: "POST", body, key }), "Capture was refused."); await refreshAuthorizations(); }
        catch (error) { showMessage($("#authorization-notices"), "authorization-error", error.message || "Capture was refused."); await refreshAuthorizations(); }
        finally { capture.disabled = false; }
      });
      actions.append(captureInput.wrap, finalOption, capture); row.append(actions);
    } else if (authorization.status === "open" && !incoming) {
      const actions = node("div", "row-actions");
      const cancel = testNode("button", `authorization-void-${id}`, "quiet-button", "Release hold"); cancel.type = "button";
      cancel.addEventListener("click", async () => {
        cancel.disabled = true; clearMessage($("#authorization-notices"), "authorization-error");
        try { requireOk(await api(`/authorizations/${encodeURIComponent(id)}/void`, { method: "POST", body: {} }), "Hold could not be released."); await refreshAuthorizations(); }
        catch (error) { showMessage($("#authorization-notices"), "authorization-error", error.message || "Hold could not be released."); await refreshAuthorizations(); }
        finally { cancel.disabled = false; }
      });
      actions.append(cancel); row.append(actions);
    }
    return row;
  }

  // Only an exact match from the app's own routes is followed, so ?next= can't redirect off-site.
  function nextRoute() {
    const next = new URLSearchParams(location.search).get("next");
    if (next === null) return "/";
    const route = next.startsWith("/") ? next : `/${next}`;
    return uiRoutes.has(route) ? route : "/";
  }

  function buildAuthPage(mode) {
    document.body.classList.add("is-public");
    const signup = mode === "signup";
    const panel = node("section", "panel auth-card");
    const logo = node("div", "auth-brand");
    logo.innerHTML = '<a class="brand" href="/" aria-label="Morrow home"><img class="brand-logo brand-mark" src="/assets/morrow-mark.png" alt=""><span class="brand-copy"><strong>morrow</strong><small>your money for tomorrow</small></span></a>';
    panel.append(logo, node("p", "eyebrow auth-eyebrow", signup ? "A fresh start" : "Welcome back"), node("h1", "", signup ? "Create your account" : "Good to have you back"), node("p", "lede auth-lede", signup ? "Build a clearer picture of your money, one step at a time." : "Sign in to see your wallet and what comes next."));
    const form = document.createElement("form"); form.className = "form-grid auth-form"; form.noValidate = true;
    const feedback = node("div", "notice-list auth-feedback");
    feedback.setAttribute("aria-live", "polite");
    const email = field("Email address", `${mode}-email`, "email", "you@example.com");
    email.input.autocomplete = "email"; email.input.autocapitalize = "none"; email.input.spellcheck = false; email.input.required = true;
    const password = field("Password", `${mode}-password`, "password", signup ? "Create a password" : "Enter your password");
    password.input.autocomplete = signup ? "new-password" : "current-password"; password.input.required = true;
    if (signup) password.input.minLength = 8;

    const addHelp = (fieldView, id, text) => {
      const help = node("p", "field-hint auth-field-hint", text); help.id = id;
      fieldView.wrap.append(help); fieldView.input.setAttribute("aria-describedby", id);
    };
    addHelp(email, `${email.input.id}-help`, "We’ll use this email for your account.");
    addHelp(password, `${password.input.id}-help`, signup ? "Use 8 to 1,024 characters. A longer passphrase is easier to remember." : "Your password is kept private and is never shown.");

    const visibility = node("button", "password-visibility", "Show");
    visibility.dataset.testid = `${password.input.id}-visibility`;
    visibility.type = "button"; visibility.setAttribute("aria-label", "Show password");
    visibility.setAttribute("aria-controls", password.input.id); visibility.setAttribute("aria-pressed", "false");
    const passwordControl = node("div", "password-control");
    password.input.parentElement.replaceChild(passwordControl, password.input);
    passwordControl.append(password.input, visibility);
    visibility.addEventListener("click", () => {
      const show = password.input.type === "password";
      password.input.type = show ? "text" : "password";
      visibility.textContent = show ? "Hide" : "Show";
      visibility.setAttribute("aria-label", `${show ? "Hide" : "Show"} password`);
      visibility.setAttribute("aria-pressed", String(show));
    });

    let display = null;
    if (signup) {
      display = field("Name people will see", "signup-display-name", "text", "Your name");
      display.input.autocomplete = "name"; display.input.required = true;
      addHelp(display, `${display.input.id}-help`, "This is the name shown on your Morrow account.");
    }
    form.append(feedback, email.wrap);
    if (display) form.append(display.wrap);
    form.append(password.wrap);
    const submit = testNode("button", `${mode}-submit`, "primary-button auth-submit", signup ? "Create account" : "Log in");
    submit.type = "submit"; form.append(submit);
    let submitting = false;
    let redirectingAfterSignup = false;

    const clearFieldError = fieldView => {
      const error = $(`#${fieldView.input.id}-error`, fieldView.wrap);
      if (error) error.remove();
      fieldView.input.setAttribute("aria-invalid", "false");
      const help = $(`#${fieldView.input.id}-help`, fieldView.wrap);
      fieldView.input.setAttribute("aria-describedby", help.id);
    };
    const setFieldError = (fieldView, message) => {
      clearFieldError(fieldView);
      const error = node("p", "auth-field-error", message); error.id = `${fieldView.input.id}-error`;
      error.dataset.testid = error.id;
      fieldView.wrap.append(error);
      fieldView.input.setAttribute("aria-invalid", "true");
      fieldView.input.setAttribute("aria-describedby", `${fieldView.input.id}-help ${error.id}`);
    };
    const fields = display ? [email, display, password] : [email, password];
    for (const fieldView of fields) {
      fieldView.input.setAttribute("aria-invalid", "false");
      fieldView.input.addEventListener("input", () => {
        clearFieldError(fieldView);
        clearMessage(feedback, "auth-error");
      });
    }

    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (submitting) return;
      clearMessage(feedback, "auth-error"); clearMessage(feedback, "auth-success");
      for (const fieldView of fields) clearFieldError(fieldView);
      let invalid = null;
      const reject = (fieldView, message) => { setFieldError(fieldView, message); invalid ||= fieldView; };
      const emailValue = email.input.value.trim();
      if (!emailValue) reject(email, "Enter your email address.");
      else if (!/^[^\s@]+@[^\s@]+$/.test(emailValue) || (emailValue.match(/@/g) || []).length !== 1) reject(email, "Enter a valid email address.");
      if (display && !display.input.value.trim()) reject(display, "Enter the name you’d like people to see.");
      if (!password.input.value) reject(password, "Enter your password.");
      else if (signup && Array.from(password.input.value).length < 8) reject(password, "Use at least 8 characters for your password.");
      else if (signup && Array.from(password.input.value).length > 1024) reject(password, "Use no more than 1,024 characters.");
      if (invalid) { invalid.input.focus(); return; }

      submitting = true; submit.disabled = true; form.setAttribute("aria-busy", "true");
      submit.textContent = signup ? "Creating account…" : "Signing in…";
      const body = { email: emailValue, password: password.input.value };
      if (display) body.display_name = display.input.value.trim();
      try {
        const result = await api(`/auth/${mode}`, { method: "POST", body });
        if (!result.response.ok) {
          const code = result.data?.error?.code;
          let message = messageOf(result, signup ? "Could not create your account." : "Email or password is incorrect.");
          if (signup && code === "email_taken") message = "That email is already registered. Log in instead.";
          else if (signup && code === "handle_taken") message = "That email would create a handle that is already in use. Try another email address.";
          else if (result.response.status === 429) message = "There have been too many attempts. Please wait a little and try again.";
          showMessage(feedback, "auth-error", message);
          return;
        }
        state.token = result.data.token; localStorage.setItem(tokenKey, state.token);
        // Store token in sessionStorage for admin/analyst pages
        sessionStorage.setItem('session_token', state.token);
        
        // Check if user has special roles and redirect accordingly
        try {
          const meResult = await api("/me");
          if (meResult.response.ok && meResult.data) {
            const roles = meResult.data.roles || [];
            // Admin gets priority
            if (roles.includes('PLATFORM_ADMIN')) {
              location.assign('/admin.html');
              return;
            }
            // Then analyst
            if (roles.includes('PAYMENT_ANALYST')) {
              location.assign('/analyst.html');
              return;
            }
          }
        } catch (e) {
          // If /me fails, continue with normal flow
          console.error('Failed to check user roles:', e);
        }
        
        if (signup) {
          redirectingAfterSignup = true;
          submit.textContent = "Account created";
          showMessage(feedback, "auth-success", "Your account is ready. Taking you to Morrow…", "success-message");
          window.setTimeout(() => location.assign(nextRoute()), 500);
        } else location.assign(nextRoute());
      } catch (error) {
        showMessage(feedback, "auth-error", "We couldn’t reach Morrow. Check your connection and try again.");
      } finally {
        form.removeAttribute("aria-busy");
        if (!redirectingAfterSignup) {
          submitting = false; submit.disabled = false;
          submit.textContent = signup ? "Create account" : "Log in";
        }
      }
    });
    panel.append(form);
    const switcher = node("p", "auth-switch");
    if (signup) { switcher.append(document.createTextNode("Already with Morrow? ")); const link = node("a", "", "Log in"); link.href = "/login"; switcher.append(link); }
    else { switcher.append(document.createTextNode("New to Morrow? ")); const link = node("a", "", "Create an account"); link.href = "/signup"; switcher.append(link); }
    panel.append(switcher);
    main.replaceChildren(node("div", "auth-shell")); main.firstChild.append(panel);
  }

  async function init() {
    setupMobileNavigation();
    document.body.classList.remove("is-public", "is-landing");
    setCurrentNavigation();
    if (state.token) {
      try {
        const me = requireOk(await api("/me"), "Your session has expired.");
        state.me = me; state.currency = me.currency; state.minorUnits = me.minor_units;
      } catch (_) {
        localStorage.removeItem(tokenKey); state.token = null; state.me = null;
      }
    }
    addCurrentIdentity();
    const path = location.pathname;
    if (path === "/signup" || path === "/login") buildAuthPage(path.slice(1));
    else if (path === "/paypal/return") state.me ? buildPaypalReturn() : buildSignedOut("assistant");
    else if (path === "/requests") state.me ? buildRequests() : buildSignedOut("requests");
    else if (path === "/split") state.me ? buildSplit() : buildSignedOut("split");
    else if (path === "/assistant") state.me ? buildAssistant() : buildSignedOut("assistant");
    else if (path === "/authorizations") state.me ? buildAuthorizations() : buildSignedOut("authorizations");
    else if (path === "/commitments") state.me ? buildCommitments() : buildSignedOut("commitments");
    else if (path === "/activity") state.me ? buildActivityPage() : buildSignedOut("activity");
    else buildHome();
  }

  function buildCommitments() {
    main.replaceChildren(pageHeading("Planning", "Upcoming commitments.", "Review recurring bills and their next due dates."));
    const region = node("section", "panel commitments-page");
    const list = testNode("div", "commitments-page-list", "commitments-page-list");
    list.append(node("p", "muted", "Loading commitments…")); region.append(list); main.append(region);
    void api("/commitments?status=active&limit=50").then(result => {
      const data = requireOk(result, "Could not load commitments.");
      const items = (data.commitments || []).filter(item => item.next_due_date)
        .sort((a, b) => String(a.next_due_date).localeCompare(String(b.next_due_date)));
      list.replaceChildren();
      if (!items.length) { list.append(emptyState("No upcoming commitments", "Active recurring bills will appear here.")); return; }
      for (const item of items) {
        const row = node("article", "commitments-page-row");
        const copy = node("div"); copy.append(node("strong", "", item.title), node("small", "", item.category || "Recurring commitment"));
        row.append(copy, node("time", "", item.next_due_date), node("strong", "", formatMoney(item.amount_minor, item.currency, state.minorUnits)));
        list.append(row);
      }
    }).catch(error => list.replaceChildren(node("p", "error-message", error.message || "Could not load commitments.")));
  }

  function buildActivityPage() {
    main.replaceChildren(pageHeading("Wallet", "Recent activity.", "Your latest wallet payments and credits."));
    const panel = node("section", "panel activity-page");
    const region = node("div", "activity-list"); region.id = "activity-region";
    const loading = loadingState("activity-loading", "Loading activity…"); loading.hidden = false;
    region.append(loading); panel.append(region); main.append(panel);
    void readPersonalActivity(state.me && state.me.user_id).then(items => renderActivity(region, items))
      .catch(error => region.replaceChildren(node("p", "error-message", error.message || "Could not load activity.")));
  }

  function buildAssistant() {
    main.replaceChildren(pageHeading("Assistant & payments", "Payments, plans and support.",
      "Complete a money action, review your outlook, or ask Morrow a question."));
    const actionSection = node("section", "money-workflow");
    actionSection.id = "money-actions";
    actionSection.append(node("h2", "", "Money actions"),
      node("p", "field-hint", "Choose an action and complete its focused form."));
    const actionForms = buildMoneyActionForms();
    const actionGrid = node("div", "money-action-grid");
    for (const key of ["pay", "request", "reserve"]) actionGrid.append(actionForms[key]);
    actionSection.append(actionGrid); main.append(actionSection);

    const forecastSettings = node("section", "panel forecast-settings-panel");
    forecastSettings.id = "financial-outlook";
    const fullOutlook = testNode("div", "full-financial-outlook", "full-financial-outlook", "Loading your forecast…");
    fullOutlook.setAttribute("aria-live", "polite");
    forecastSettings.append(panelHeading("Financial outlook", "Morrow uses wallet history and recurring commitments to estimate what remains."),
      fullOutlook, panelHeading("Planning amounts", "These are reserved amounts in the forecast, not transactions."), buildForecastSettingsForm());
    main.append(forecastSettings);
    void api("/safe-to-spend").then(result => {
      if (result.response.ok) renderFullOutlook(fullOutlook, result.data);
      else fullOutlook.replaceChildren(node("p", "error-message", messageOf(result, "Could not load your forecast.")));
    }).catch(error => fullOutlook.replaceChildren(node("p", "error-message", error.message || "Could not load your forecast.")));
    const companion = node("section", "panel assistant-panel companion-panel");
    companion.append(node("p", "eyebrow", "AI financial companion"));
    companion.append(node("h2", "", "Understand your money"));
    companion.append(node("p", "assistant-note", "Answers use your verified Morrow wallet and finance summaries. Amounts and forecasts are calculated by Morrow; the assistant cannot change your records."));
    const companionForm = document.createElement("form"); companionForm.className = "form-stack";
    const companionInput = field("Ask a financial question", "companion-message", "text",
      "Why is my Safe-to-Spend amount unavailable?", "companion-message");
    companionInput.input.maxLength = 1000; companionInput.input.required = true;
    const suggestions = node("div", "assistant-suggestions");
    for (const question of ["Why is my Safe-to-Spend amount this month?", "What payments should I prepare for?",
      "Where am I spending the most?", "How much do I owe or expect to receive?",
      "What happens if I spend 25?"]) {
      const suggestion = node("button", "secondary-button", question); suggestion.type = "button";
      suggestion.addEventListener("click", () => { companionInput.input.value = question; companionInput.input.focus(); });
      suggestions.append(suggestion);
    }
    const companionSubmit = node("button", "primary-button", "Ask Morrow"); companionSubmit.type = "submit";
    companionForm.append(companionInput.wrap, suggestions, companionSubmit);
    const companionResponse = node("section", "assistant-response companion-response");
    companionResponse.setAttribute("aria-live", "polite"); companionResponse.setAttribute("aria-busy", "false");
    companion.append(companionForm, companionResponse); main.append(companion);
    companionForm.addEventListener("submit", async event => {
      event.preventDefault(); companionSubmit.disabled = true;
      companionResponse.setAttribute("aria-busy", "true");
      companionResponse.replaceChildren(node("p", "muted", "Reviewing your Morrow financial snapshot…"));
      try {
        const result = requireOk(await api("/ai/companion", {method: "POST", body: {message: companionInput.input.value}}),
          "Morrow could not answer that question. Your financial records were not changed.");
        companionResponse.replaceChildren();
        companionResponse.append(node("span", "status-pill", result.provider === "gemini" ? "Gemini explanation" : "Verified data · AI unavailable"));
        companionResponse.append(node("p", "companion-answer", result.answer));
        const context = result.financial_context || {};
        const facts = node("div", "companion-facts");
        facts.append(node("h3", "", "Verified snapshot"));
        const wallet = context.wallet || {};
        facts.append(outlookRow("Wallet balance", formatMoney(wallet.balance_minor || 0, context.currency, context.minor_units)));
        facts.append(outlookRow("Available after holds", formatMoney(wallet.available_after_holds_minor || 0, context.currency, context.minor_units)));
        facts.append(outlookRow("On hold", formatMoney(wallet.holds_minor || 0, context.currency, context.minor_units)));
        const safe = context.safe_to_spend || {};
        facts.append(outlookRow("Safe-to-Spend", safe.status === "available"
          ? formatMoney(safe.amount_minor, context.currency, context.minor_units) : "Unavailable"));
        if (safe.status !== "available") facts.append(node("p", "field-hint", formatForecastReasons(safe.unavailable_reasons || [])));
        const forecast = context.forecast || {};
        if (Number.isInteger(forecast.remaining_variable_spend_minor)) {
          facts.append(outlookRow("Forecast remaining variable spend", formatMoney(forecast.remaining_variable_spend_minor, context.currency, context.minor_units)));
        }
        const scenario = context.hypothetical_scenario;
        if (scenario) {
          facts.append(node("h4", "", "Hypothetical · not a transaction"));
          facts.append(outlookRow("Additional spend", formatMoney(scenario.amount_minor, context.currency, context.minor_units)));
          facts.append(outlookRow("Safe-to-Spend after this spend", scenario.status === "available"
            ? formatMoney(scenario.resulting_safe_to_spend_minor, context.currency, context.minor_units) : "Unavailable"));
        }
        const requests = context.pending_p2p_requests || {};
        facts.append(outlookRow("Pending requests you owe", formatMoney(requests.amount_owed_minor || 0, context.currency, context.minor_units)));
        facts.append(outlookRow("Pending requests to receive", formatMoney(requests.amount_receivable_minor || 0, context.currency, context.minor_units)));
        const shared = context.shared_expenses || {};
        facts.append(outlookRow("Shared expense reimbursements outstanding", formatMoney(shared.outstanding_receivable_minor || 0, context.currency, context.minor_units)));
        const transactions = context.transactions || {};
        facts.append(node("h4", "", "Spending categories"));
        if (transactions.category_status === "available" && Object.keys(transactions.outgoing_by_category_minor || {}).length) {
          for (const [category, amount] of Object.entries(transactions.outgoing_by_category_minor)) {
            facts.append(outlookRow(category, formatMoney(amount, context.currency, context.minor_units)));
          }
        } else facts.append(node("p", "field-hint", "Wallet transactions do not have category labels, so Morrow cannot identify a top spending category yet."));
        facts.append(node("h4", "", "Recent wallet transactions"));
        const recent = transactions.recent || [];
        if (recent.length) {
          for (const item of recent.slice(-8).reverse()) facts.append(outlookRow(
            `${item.date} · ${item.direction}${item.category ? ` · ${item.category}` : ""}`,
            formatMoney(item.amount_minor, context.currency, context.minor_units)));
        } else facts.append(node("p", "field-hint", "No recent transactions in the current month."));
        const commitments = context.upcoming_commitments || [];
        facts.append(node("h4", "", "Upcoming commitments (next 30 days)"));
        if (commitments.length) {
          for (const item of commitments.slice(0, 8)) facts.append(outlookRow(
            `${item.due_date} · ${item.category || "Commitment"}`, formatMoney(item.amount_minor, context.currency, context.minor_units)));
        } else facts.append(node("p", "field-hint", "No unpaid commitments are due in this period."));
        const assumptions = forecast.assumptions || [];
        facts.append(node("p", "field-hint", assumptions.join(" ")));
        facts.append(node("p", "field-hint", result.disclaimer || ""));
        companionResponse.append(facts);
      } catch (error) {
        companionResponse.replaceChildren(node("p", "error-message", error.message || "The assistant is unavailable. Try again shortly."));
      } finally { companionResponse.setAttribute("aria-busy", "false"); companionSubmit.disabled = false; }
    });

    const panel = node("section", "panel assistant-panel");
    panel.append(node("p", "eyebrow", "Offline-safe assistant · deterministic fallback"));
    panel.append(node("p", "assistant-note", "The assistant drafts social expense requests. It never sends or transfers money."));
    const form = document.createElement("form"); form.className = "form-stack";
    const prompt = field("What would you like to do?", "assistant-message", "text",
      "Split €48.00 dinner with me, alex, rahul and priya", "assistant-message");
    prompt.input.required = true; prompt.input.maxLength = 1000;
    const submit = node("button", "primary-button", "Create proposal"); submit.type = "submit";
    form.append(prompt.wrap, submit);
    const response = node("div", "assistant-response"); response.setAttribute("aria-live", "polite");
    const history = node("section", "checkout-history");
    history.append(panelHeading("Sandbox checkouts", "Actual PayPal provider state; this is merchant checkout, not a friend-to-friend transfer."));
    const historyList = node("div", "checkout-list"); history.append(historyList);
    panel.append(form, response, history); main.append(panel);
    void refreshCheckoutHistory(historyList);
    form.addEventListener("submit", async event => {
      event.preventDefault(); submit.disabled = true;
      response.replaceChildren(node("p", "muted", "Checking your request…"));
      try {
        const proposal = requireOk(await api("/ai/proposals", {method:"POST", body:{message:prompt.input.value}}), "Could not prepare this proposal.");
        response.replaceChildren();
        if (proposal.action === "balance_query") {
          response.append(node("h2", "", `Outstanding requests: ${formatMoney(proposal.amount_owed_minor, proposal.currency)}`),
            node("p", "", proposal.explanation)); return;
        }
        response.append(node("h2", "", proposal.action === "split" ? "Review the split" : proposal.action === "paypal_checkout" ? "Review PayPal Sandbox checkout" : "Review the request"));
        if (proposal.shares) {
          const list = node("ul", "assistant-shares");
          for (const share of proposal.shares) list.append(node("li", "", `${share.handle} · ${formatMoney(share.amount, proposal.currency)}`));
          response.append(list);
        } else if (proposal.action === "paypal_checkout") {
          response.append(node("p", "", `Amount: ${formatMoney(proposal.amount, proposal.currency)}`));
          response.append(node("p", "assistant-note", `Recipient: ${proposal.merchant_label || "Morrow Sandbox merchant"}. This is for ${proposal.context_handle}; PayPal will pay the merchant account connected to this Sandbox app. It will not transfer money to ${proposal.context_handle}.`));
        } else response.append(node("p", "", `${proposal.payer_handle} will be asked for ${formatMoney(proposal.amount, proposal.currency)}.`));
        response.append(node("p", "assistant-note", proposal.explanation));
        const confirm = node("button", "primary-button", proposal.action === "paypal_checkout" ? "Confirm and continue to PayPal" : "Confirm and create requests"); confirm.type = "button";
        const confirmationKey = freshKey();
        response.append(confirm);
        confirm.addEventListener("click", async () => {
          confirm.disabled = true;
          try {
            requireOk(await api(`/ai/proposals/${encodeURIComponent(proposal.proposal_id)}/confirm`,
              {method:"POST", body:{confirm:true}, key:confirmationKey}), "Could not confirm this proposal.");
            if (result.result && result.result.approval_url) {
              location.assign(result.result.approval_url); return;
            }
            if (proposal.action === "paypal_checkout") {
              const checkout = result.result || {};
              response.replaceChildren(node("h2", "", `PayPal checkout status: ${checkout.status || "unknown"}`),
                node("p", "assistant-note", checkout.status === "FAILED"
                  ? "PayPal did not create an approvable order. Start a new checkout with a new confirmation."
                  : "No approval link was returned, so Morrow has not redirected you or represented this as paid."));
              void refreshCheckoutHistory(historyList); return;
            }
            response.replaceChildren(node("h2", "", "Requests created"), node("p", "", "No money moved. Track replies in Requests."));
            const link = node("a", "secondary-button", "View requests"); link.href = "/requests"; response.append(link);
            void refreshCheckoutHistory(historyList);
          } catch (error) { response.append(node("p", "error-message", error.message)); confirm.disabled = false; }
        });
      } catch (error) { response.replaceChildren(node("p", "error-message", error.message)); }
      finally { submit.disabled = false; }
    });
    const requestedSection = decodeURIComponent(location.hash.slice(1));
    if (requestedSection) setTimeout(() => document.getElementById(requestedSection)?.scrollIntoView({block: "start"}), 0);
  }

  async function refreshCheckoutHistory(list) {
    try {
      const data = requireOk(await api("/paypal/checkouts"), "Could not load Sandbox checkout history.");
      list.replaceChildren();
      if (!data.checkouts.length) { list.append(emptyState("No Sandbox checkouts yet", "Confirmed PayPal orders will appear here.")); return; }
      for (const checkout of data.checkouts) {
        const row = node("article", "checkout-row");
        const top = node("div", "row-top");
        top.append(node("strong", "row-title", `${formatMoney(checkout.amount_minor, checkout.currency)} · ${checkout.merchant_label}`));
        const badge = node("span", `status-pill status-${checkout.status.toLowerCase()}`, checkout.status.replaceAll("_", " "));
        top.append(badge); row.append(top);
        row.append(node("p", "row-meta", checkout.note || "PayPal Sandbox merchant checkout"));
        row.append(node("p", "row-meta", `Order ${checkout.order_id || "pending"}${checkout.capture_id ? ` · Capture ${checkout.capture_id}` : ""}`));
        if (checkout.approval_url) {
          const approve = node("a", "secondary-button", "Continue PayPal approval");
          approve.href = checkout.approval_url; row.append(approve);
        }
        if (["APPROVED", "CAPTURE_UNKNOWN"].includes(checkout.status)) {
          const capture = node("button", "primary-button", checkout.status === "APPROVED"
            ? "Capture after PayPal approval" : "Retry capture safely");
          capture.type = "button";
          capture.addEventListener("click", async () => {
            capture.disabled = true;
            try {
              requireOk(await api(`/paypal/checkouts/${encodeURIComponent(checkout.checkout_id)}/capture`,
                {method:"POST", body:{}, key:freshKey()}), "PayPal capture is not complete yet.");
              await refreshCheckoutHistory(list);
            } catch (error) { capture.disabled = false; row.append(node("p", "error-message", error.message)); }
          });
          row.append(capture);
        }
        if (checkout.provider_error) row.append(node("p", "error-message", checkout.provider_error));
        const reconcile = node("button", "quiet-button", "Refresh provider status"); reconcile.type = "button";
        reconcile.addEventListener("click", async () => {
          reconcile.disabled = true;
          try { await api(`/paypal/checkouts/${encodeURIComponent(checkout.checkout_id)}/reconcile`, {method:"POST", body:{}}); await refreshCheckoutHistory(list); }
          catch (_) { reconcile.disabled = false; }
        });
        row.append(reconcile); list.append(row);
      }
    } catch (_) { list.replaceChildren(node("p", "error-message", "Could not load checkout history.")); }
  }

  function buildPaypalReturn() {
    main.replaceChildren(pageHeading("PayPal Sandbox", "Verifying checkout status", "Morrow verifies approval with PayPal before requesting capture."));
    const panel = node("section", "panel");
    const result = node("div", "checkout-return-state", "Checking the order directly with PayPal…");
    panel.append(result); main.append(panel);
    const checkoutId = new URLSearchParams(location.search).get("checkout_id");
    if (!checkoutId) { result.textContent = "Checkout reference is missing."; return; }
    if (new URLSearchParams(location.search).get("cancelled") === "1") {
      result.textContent = "PayPal returned from the approval page without approval. No capture was requested.";
      return;
    }
    const keyName = `pocketful.paypal.capture.${checkoutId}`;
    let captureKey = sessionStorage.getItem(keyName);
    if (!captureKey) { captureKey = freshKey(); sessionStorage.setItem(keyName, captureKey); }
    void api(`/paypal/checkouts/${encodeURIComponent(checkoutId)}/capture`,
      {method:"POST", body:{}, key:captureKey}).then(response => {
        const checkout = requireOk(response, "PayPal has not confirmed approval or capture.");
        result.replaceChildren(node("strong", `status-pill status-${checkout.status.toLowerCase()}`, checkout.status.replaceAll("_", " ")),
          node("p", "", checkout.status === "COMPLETED" ? "PayPal confirmed the merchant capture." : "PayPal received the capture request; status is not yet final."),
          node("p", "row-meta", `Order ${checkout.order_id} · Capture ${checkout.capture_id || "pending"}`));
        sessionStorage.removeItem(keyName);
      }).catch(error => {
        result.replaceChildren(node("p", "error-message", error.message));
        const link = node("a", "secondary-button", "Return to Morrow"); link.href = "/assistant"; result.append(link);
      });
  }

  function buildSignedOut(destination) {
    document.body.classList.add("is-public");
    main.replaceChildren(pageHeading("Morrow", "Your wallet is waiting.", "Log in to see your requests and payment activity."));
    const panel = node("section", "panel");
    const link = node("a", "primary-button", "Log in"); link.href = `/login?next=${encodeURIComponent(destination)}`;
    panel.append(link); main.append(panel);
  }

  void init();
})();
