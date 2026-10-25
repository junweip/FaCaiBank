"use strict";

const CONFIG = Object.assign(
  { apiBase: "/api", region: "ap-southeast-1", userPoolId: "", clientId: "", version: "dev" },
  window.FACAI_CONFIG || {},
);
const COGNITO_URL = `https://cognito-idp.${CONFIG.region}.amazonaws.com/`;
const SESSION_KEY = "facai.session";
const THEME_KEY = "facai.theme";
const OTHER_RECIPIENT = "__other__";
const ACCOUNT_LIMITS = { EVERYDAY: 2, SAVINGS: 2 };
const ROUTES = { overview: "Overview", transfers: "Transfers", activity: "Activity", documents: "Documents" };

const state = {
  session: null,
  profile: null,
  accounts: new Map(),
  selectedAccountId: "",
  banks: [],
  pendingAuth: null,
  pendingSignUp: null,
  stepUp: null,
  statementTimer: null,
  statementStatus: new Map(),
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(attributes).forEach(([key, value]) => {
    if (key === "className") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  });
  children.flat().forEach((child) => node.append(child instanceof Node ? child : String(child)));
  return node;
}

function money(value) {
  const amount = Number(value || 0);
  const formatted = Math.abs(amount).toLocaleString("en-SG", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${amount < 0 ? "−" : ""}S$${formatted}`;
}

function typeLabel(type) {
  return type === "SAVINGS" ? "Savings" : "Everyday";
}

function accountLabel(account) {
  return `${account.nickname || typeLabel(account.account_type)} · ${account.account_id.slice(0, 8)}`;
}

/* ---------------------------------------------------------------- notifications */

const TOAST_ICONS = { success: "✓", error: "!", info: "i" };

function toast(type, message, options = {}) {
  const container = $("#toasts");
  const node = el(
    "div",
    { className: `toast toast-${type}`, role: type === "error" ? "alert" : "status" },
    el("span", { className: "toast-icon", "aria-hidden": "true", text: TOAST_ICONS[type] }),
    el("span", { className: "toast-text", text: message }),
  );
  let timer;
  const dismiss = () => {
    window.clearTimeout(timer);
    node.classList.add("leaving");
    window.setTimeout(() => node.remove(), 200);
  };
  const schedule = (duration) => {
    window.clearTimeout(timer);
    if (duration > 0) timer = window.setTimeout(dismiss, duration);
  };
  node.addEventListener("click", dismiss);
  container.prepend(node);
  while (container.children.length > 4) container.lastElementChild.remove();
  schedule(options.duration ?? (type === "error" ? 7000 : 4500));
  return {
    dismiss,
    update(nextType, nextMessage) {
      node.className = `toast toast-${nextType}`;
      node.setAttribute("role", nextType === "error" ? "alert" : "status");
      node.querySelector(".toast-icon").textContent = TOAST_ICONS[nextType];
      node.querySelector(".toast-text").textContent = nextMessage;
      schedule(nextType === "error" ? 7000 : 4500);
    },
  };
}

const notify = {
  success: (message, options) => toast("success", message, options),
  error: (message, options) => toast("error", message, options),
  info: (message, options) => toast("info", message, options),
};

/* ---------------------------------------------------------------- storage and theme */

function saveSession() {
  try {
    if (state.session) window.sessionStorage.setItem(SESSION_KEY, JSON.stringify(state.session));
    else window.sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* storage unavailable: the session lives in memory only */
  }
}

function loadSession() {
  try {
    return JSON.parse(window.sessionStorage.getItem(SESSION_KEY) || "null");
  } catch {
    return null;
  }
}

function currentTheme() {
  const explicit = document.documentElement.getAttribute("data-theme");
  if (explicit) return explicit;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try {
    window.localStorage.setItem(THEME_KEY, next);
  } catch {
    /* remembered for this page only */
  }
  notify.info(`${next === "dark" ? "Dark" : "Light"} mode on.`, { duration: 2000 });
}

/* ---------------------------------------------------------------- Cognito */

class CognitoError extends Error {
  constructor(type, message) {
    super(message);
    this.type = type;
  }
}

async function cognito(action, body) {
  if (!CONFIG.clientId) throw new Error("Sign-in is not configured for this environment.");
  let response;
  try {
    response = await fetch(COGNITO_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": `AWSCognitoIdentityProviderService.${action}`,
      },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error("Could not reach the sign-in service. Check your connection.");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const type = String(payload.__type || "").split("#").pop();
    throw new CognitoError(type, friendlyCognitoMessage(type, payload.message));
  }
  return payload;
}

function friendlyCognitoMessage(type, message) {
  const messages = {
    NotAuthorizedException: "Incorrect email or password.",
    CodeMismatchException: "That code didn't match. Try again.",
    EnableSoftwareTokenMFAException: "That code didn't match. Try again.",
    ExpiredCodeException: "That code has expired. Request a new one.",
    UsernameExistsException: "An account with this email already exists. Sign in instead.",
    InvalidPasswordException: "Use at least 12 characters with upper and lower case letters, a number and a symbol.",
    LimitExceededException: "Too many attempts. Wait a few minutes and try again.",
    TooManyRequestsException: "Too many attempts. Wait a few minutes and try again.",
    UserNotFoundException: "Incorrect email or password.",
  };
  return messages[type] || message || "Sign-in failed. Please try again.";
}

function decodeJwt(token) {
  const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  return JSON.parse(atob(part.padEnd(part.length + ((4 - (part.length % 4)) % 4), "=")));
}

function storeTokens(result, email) {
  const claims = decodeJwt(result.IdToken);
  state.session = {
    accessToken: result.AccessToken,
    idToken: result.IdToken,
    refreshToken: result.RefreshToken || state.session?.refreshToken,
    expiresAt: Date.now() + Number(result.ExpiresIn || 3600) * 1000,
    email: claims.email || email,
  };
  saveSession();
}

async function handleAuthResult(result, email) {
  if (result.AuthenticationResult) {
    storeTokens(result.AuthenticationResult, email);
    state.pendingAuth = null;
    return "done";
  }
  state.pendingAuth = {
    email,
    session: result.Session,
    username: result.ChallengeParameters?.USER_ID_FOR_SRP || email,
  };
  if (result.ChallengeName === "SOFTWARE_TOKEN_MFA") return "mfa";
  if (result.ChallengeName === "MFA_SETUP") {
    const association = await cognito("AssociateSoftwareToken", { Session: result.Session });
    state.pendingAuth.session = association.Session;
    state.pendingAuth.secret = association.SecretCode;
    return "mfa_setup";
  }
  throw new Error(`Unsupported sign-in step: ${result.ChallengeName}`);
}

function signIn(email, password) {
  return cognito("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: CONFIG.clientId,
    AuthParameters: { USERNAME: email, PASSWORD: password },
  }).then((result) => handleAuthResult(result, email));
}

async function answerMfa(code) {
  const pending = state.pendingAuth;
  const result = await cognito("RespondToAuthChallenge", {
    ChallengeName: "SOFTWARE_TOKEN_MFA",
    ClientId: CONFIG.clientId,
    Session: pending.session,
    ChallengeResponses: { USERNAME: pending.username, SOFTWARE_TOKEN_MFA_CODE: code },
  });
  return handleAuthResult(result, pending.email);
}

async function completeMfaSetup(code) {
  const pending = state.pendingAuth;
  const verified = await cognito("VerifySoftwareToken", {
    Session: pending.session,
    UserCode: code,
    FriendlyDeviceName: "FaCaiBank web",
  });
  const result = await cognito("RespondToAuthChallenge", {
    ChallengeName: "MFA_SETUP",
    ClientId: CONFIG.clientId,
    Session: verified.Session,
    ChallengeResponses: { USERNAME: pending.username },
  });
  return handleAuthResult(result, pending.email);
}

async function freshAccessToken() {
  if (!state.session) throw new Error("Please sign in.");
  if (Date.now() < state.session.expiresAt - 60_000) return state.session.accessToken;
  try {
    const result = await cognito("InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH",
      ClientId: CONFIG.clientId,
      AuthParameters: { REFRESH_TOKEN: state.session.refreshToken },
    });
    storeTokens(result.AuthenticationResult, state.session.email);
  } catch {
    signOutLocally();
    throw new Error("Your session ended. Please sign in again.");
  }
  return state.session.accessToken;
}

async function stepUp(password, code) {
  const email = state.session.email;
  const first = await cognito("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: CONFIG.clientId,
    AuthParameters: { USERNAME: email, PASSWORD: password },
  });
  if (first.AuthenticationResult) return storeTokens(first.AuthenticationResult, email);
  if (first.ChallengeName !== "SOFTWARE_TOKEN_MFA") throw new Error("Unexpected sign-in step during confirmation.");
  const result = await cognito("RespondToAuthChallenge", {
    ChallengeName: "SOFTWARE_TOKEN_MFA",
    ClientId: CONFIG.clientId,
    Session: first.Session,
    ChallengeResponses: { USERNAME: first.ChallengeParameters?.USER_ID_FOR_SRP || email, SOFTWARE_TOKEN_MFA_CODE: code },
  });
  storeTokens(result.AuthenticationResult, email);
}

/* ---------------------------------------------------------------- API */

class ApiError extends Error {
  constructor(status, message, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function detailMessage(detail, status) {
  if (!detail) return status >= 500 ? "Something went wrong on our side. Please try again." : `Request failed (${status})`;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg.replace(/^Value error, /, "")).join(", ");
  return detail.message || `Request failed (${status})`;
}

async function api(path, options = {}) {
  const { method = "GET", body, formData, idempotencyKey, allowStepUp = true } = options;
  const headers = { Authorization: `Bearer ${await freshAccessToken()}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  let response;
  try {
    response = await fetch(`${CONFIG.apiBase}${path}`, {
      method,
      headers,
      body: formData || (body !== undefined ? JSON.stringify(body) : undefined),
    });
  } catch {
    throw new Error("Could not reach FaCai Bank. Check your connection and try again.");
  }
  const payload = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (response.ok) return payload;

  const detail = payload.detail;
  if (response.status === 401 && detail?.code === "step_up_required" && allowStepUp) {
    await requestStepUp(detail);
    return api(path, { ...options, allowStepUp: false });
  }
  if (response.status === 401) {
    signOutLocally();
    throw new ApiError(401, "Your session ended. Please sign in again.");
  }
  throw new ApiError(response.status, detailMessage(detail, response.status), detail?.code);
}

function requestStepUp(detail) {
  const reasons = {
    high_value_transfer: "Transfers of S$1,000 or more need you to confirm it's you.",
    high_value_withdrawal: "Withdrawals of S$1,000 or more need you to confirm it's you.",
    unusual_transfer: "This transfer looks different from your usual activity, so we need to confirm it's you.",
  };
  $("#step-up-reason").textContent = reasons[detail.reason] || detail.message;
  notify.info("Please confirm it's you to continue.");
  return new Promise((resolve, reject) => {
    state.stepUp = { resolve, reject };
    $("#step-up-modal").showModal();
  });
}

/* ---------------------------------------------------------------- routing */

function currentRoute() {
  const route = window.location.hash.replace(/^#\/?/, "").split("?")[0];
  return ROUTES[route] ? route : "overview";
}

function showRoute() {
  const route = currentRoute();
  $$("[data-page]").forEach((page) => {
    page.hidden = page.dataset.page !== route;
  });
  $$(".nav-link").forEach((link) => link.classList.toggle("active", link.dataset.route === route));
  $("#page-title").textContent = ROUTES[route];
  document.title = `${ROUTES[route]} · FaCai Bank`;
  if (!state.profile) return;
  const loaders = {
    overview: () => Promise.all([refreshBalance(), loadRecent()]),
    transfers: () => refreshAllBalances(),
    activity: () => loadActivity(),
    documents: () => loadStatements(),
  };
  loaders[route]().catch((error) => notify.error(error.message));
}

/* ---------------------------------------------------------------- session views */

function showAuthPanel(id) {
  $$(".auth-panel").forEach((panel) => {
    panel.hidden = panel.id !== id;
  });
  $$(".auth-tabs .segment").forEach((tab) => tab.classList.toggle("active", tab.dataset.authTab === id));
  const first = $(`#${id} input`);
  if (first) first.focus();
}

function showMfaSetup() {
  const { secret, email } = state.pendingAuth;
  $("#mfa-secret").textContent = secret;
  const uri = `otpauth://totp/FaCaiBank:${encodeURIComponent(email)}?secret=${secret}&issuer=FaCaiBank`;
  if (window.qrcode) {
    const qr = window.qrcode(0, "M");
    qr.addData(uri);
    qr.make();
    $("#mfa-qr").src = qr.createDataURL(4, 2);
  }
  showAuthPanel("mfa-setup-form");
}

async function afterSignInStep(step) {
  if (step === "mfa") {
    showAuthPanel("mfa-form");
    return "Enter the code from your authenticator app.";
  }
  if (step === "mfa_setup") {
    showMfaSetup();
    return "Set up your authenticator app to finish signing in.";
  }
  await enterApp();
  return "You're signed in.";
}

function signOutLocally() {
  state.session = null;
  state.profile = null;
  state.accounts.clear();
  state.selectedAccountId = "";
  window.clearInterval(state.statementTimer);
  saveSession();
  document.body.classList.add("signed-out");
  $("#app-view").hidden = true;
  $$("dialog[open]").forEach((dialog) => dialog.close());
  showAuthPanel("signin-form");
}

async function signOut() {
  const refreshToken = state.session?.refreshToken;
  try {
    await api("/auth/logout", { method: "POST", allowStepUp: false });
  } catch {
    /* the local session is cleared regardless */
  }
  if (refreshToken) {
    cognito("RevokeToken", { Token: refreshToken, ClientId: CONFIG.clientId }).catch(() => {});
  }
  signOutLocally();
  notify.info("You're signed out. See you soon.");
}

async function enterApp() {
  document.body.classList.remove("signed-out");
  $("#app-view").hidden = false;
  $("#version-label").textContent = `Prototype · ${CONFIG.version}`;
  if (!window.location.hash) window.history.replaceState(null, "", "#/overview");
  try {
    if (await loadProfile()) await afterProfile();
  } catch (error) {
    notify.error(error.message);
  }
}

async function loadProfile() {
  try {
    state.profile = await api("/me");
  } catch (error) {
    if (error.status === 404 && error.code === "profile_required") {
      $("#profile-modal").showModal();
      return false;
    }
    throw error;
  }
  renderProfile();
  syncAccounts(state.profile.accounts);
  return true;
}

async function afterProfile() {
  loadBanks().catch(() => {});
  refreshAlertDot().catch(() => {});
  await refreshAllBalances();
  showRoute();
}

function renderProfile() {
  const profile = state.profile;
  const initials = profile.full_name.split(/\s+/).map((part) => part[0]).slice(0, 2).join("").toUpperCase();
  $("#profile-initials").textContent = initials || "FC";
  $("#profile-name").textContent = profile.full_name;
  $("#profile-email").textContent = profile.email;
  const verified = profile.kyc_status === "VERIFIED_MOCK";
  $("#kyc-label").textContent = verified ? "Identity verified" : "Identity check pending";
  $("#kyc-status").textContent = verified ? "Verified" : "Pending";
  $("#kyc-chip").classList.toggle("verified", verified);
  $("#kyc-status-chip").classList.toggle("verified", verified);
  $("#kyc-banner").hidden = verified;
  $("#kyc-form").hidden = verified;
}

/* ---------------------------------------------------------------- accounts */

function accountCounts() {
  const counts = { EVERYDAY: 0, SAVINGS: 0 };
  state.accounts.forEach((account) => {
    counts[account.account_type] = (counts[account.account_type] || 0) + 1;
  });
  return counts;
}

function syncAccounts(list) {
  const previous = state.accounts;
  state.accounts = new Map(
    list.map((account) => [account.account_id, { ...account, balance: previous.get(account.account_id)?.balance }]),
  );
  if (!state.accounts.has(state.selectedAccountId)) {
    state.selectedAccountId = state.accounts.keys().next().value || "";
  }
  renderAccounts();
}

function renderAccounts() {
  const switcher = $("#account-switcher");
  switcher.replaceChildren();
  state.accounts.forEach((account) => {
    const chip = el(
      "button",
      {
        type: "button",
        role: "tab",
        className: `account-chip${account.account_id === state.selectedAccountId ? " selected" : ""}`,
        "aria-selected": String(account.account_id === state.selectedAccountId),
      },
      el("span", { className: "chip-type", text: typeLabel(account.account_type).toUpperCase() }),
      el("span", { className: "chip-name", text: account.nickname || `${typeLabel(account.account_type)} account` }),
      el("span", { className: "chip-balance", text: account.balance === undefined ? "…" : money(account.balance) }),
    );
    chip.addEventListener("click", () => selectAccount(account.account_id, true));
    switcher.append(chip);
  });
  const counts = accountCounts();
  const full = counts.EVERYDAY >= ACCOUNT_LIMITS.EVERYDAY && counts.SAVINGS >= ACCOUNT_LIMITS.SAVINGS;
  const add = el("button", { type: "button", className: "account-chip add", text: full ? "Account limit reached" : "＋ Open an account" });
  add.disabled = full;
  add.addEventListener("click", () => openAccountModal());
  switcher.append(add);

  const selected = state.accounts.get(state.selectedAccountId);
  $("#balance-amount").textContent = money(selected?.balance);
  $("#account-summary").textContent = selected ? accountLabel(selected) : "Open an account to get started";
  updateAccountSelectors();
  updateAvailableHints();
}

function updateAccountSelectors() {
  $$(".account-options").forEach((select) => {
    const selected = select.value;
    const includeOther = select.dataset.includeOther === "true";
    select.replaceChildren(new Option(includeOther ? "Select a recipient" : "Select an account", ""));
    const group = includeOther ? el("optgroup", { label: "My accounts" }) : select;
    state.accounts.forEach((account) => group.append(new Option(accountLabel(account), account.account_id)));
    if (includeOther) select.append(group, new Option("Another FaCaiBank account…", OTHER_RECIPIENT));
    if ([...select.options].some((option) => option.value === selected && selected)) select.value = selected;
    else if (!includeOther && state.selectedAccountId) select.value = state.selectedAccountId;
  });
}

function availableBalance(accountId) {
  const balance = state.accounts.get(accountId)?.balance;
  return balance === undefined ? null : Number(balance);
}

function updateAvailableHints() {
  const transferFrom = $('#transfer-form select[name="sender_account_id"]').value;
  const moneyAccount = $('#money-form select[name="account_id"]').value;
  $("#transfer-available").textContent = `Available: ${money(availableBalance(transferFrom))}`;
  $("#money-available").textContent = `Available: ${money(availableBalance(moneyAccount))}`;
}

async function selectAccount(accountId, announce = false) {
  if (!accountId || !state.accounts.has(accountId)) return;
  const changed = accountId !== state.selectedAccountId;
  state.selectedAccountId = accountId;
  renderAccounts();
  if (announce && changed) notify.info(`Showing ${accountLabel(state.accounts.get(accountId))}.`, { duration: 2000 });
  try {
    await Promise.all([refreshBalance(accountId), loadRecent(accountId)]);
  } catch (error) {
    notify.error(error.message);
  }
}

async function refreshBalance(accountId = state.selectedAccountId) {
  if (!accountId || !state.accounts.has(accountId)) return;
  const result = await api(`/accounts/${encodeURIComponent(accountId)}/balance`);
  state.accounts.get(accountId).balance = result.balance;
  renderAccounts();
}

async function refreshAllBalances() {
  await Promise.all([...state.accounts.keys()].map((accountId) => refreshBalance(accountId).catch(() => {})));
}

function openAccountModal() {
  const counts = accountCounts();
  const select = $("#account-type-select");
  let firstAvailable = "";
  [...select.options].forEach((option) => {
    const remaining = ACCOUNT_LIMITS[option.value] - (counts[option.value] || 0);
    option.disabled = remaining <= 0;
    option.textContent = `${typeLabel(option.value)} (${counts[option.value] || 0} of ${ACCOUNT_LIMITS[option.value]})`;
    if (!option.disabled && !firstAvailable) firstAvailable = option.value;
  });
  if (!firstAvailable) {
    notify.error("You already have 2 everyday and 2 savings accounts, the most allowed.");
    return;
  }
  select.value = firstAvailable;
  $("#account-limit-hint").textContent = "Greyed-out types have reached their limit.";
  $("#account-modal").showModal();
}

/* ---------------------------------------------------------------- activity */

function describeEntry(entry) {
  const bank = String(entry.counterparty_account_id || "").replace("EXTERNAL:", "");
  const descriptions = {
    "TRANSFER:DEBIT": "Transfer sent",
    "TRANSFER:CREDIT": "Transfer received",
    "DEPOSIT:CREDIT": `Deposit from ${bank}`,
    "WITHDRAWAL:DEBIT": `Withdrawal to ${bank}`,
    "REVERSAL:CREDIT": `Withdrawal returned by ${bank}`,
    "ACCOUNT_CLOSURE:CREDIT": "Balance moved from a closed account",
    "ACCOUNT_CLOSURE:DEBIT": "Account closed",
  };
  return descriptions[`${entry.transaction_type}:${entry.entry_type}`] || entry.transaction_type;
}

function renderActivity(container, entries, emptyText) {
  container.replaceChildren();
  if (!entries.length) {
    container.append(el("div", { className: "empty" }, el("strong", { text: "No activity yet" }), el("span", { text: emptyText })));
    return;
  }
  entries.forEach((entry) => {
    const isDebit = entry.entry_type === "DEBIT";
    const status = { PENDING_EXTERNAL: "Pending", REVERSED: "Reversed" }[entry.status];
    const title = el("strong", { text: describeEntry(entry) });
    if (status) title.append(el("span", { className: "tag", text: status }));
    container.append(
      el(
        "div",
        { className: "activity-row" },
        el("span", { className: `activity-symbol${isDebit ? " debit" : ""}`, "aria-hidden": "true", text: isDebit ? "↗" : "↙" }),
        el(
          "span",
          { className: "row-copy" },
          title,
          el("span", { text: `${new Date(entry.created_at).toLocaleString("en-SG", { dateStyle: "medium", timeStyle: "short" })} · ${entry.transaction_id.slice(0, 8)}` }),
        ),
        el("span", { className: `row-amount${isDebit ? "" : " credit"}`, text: `${isDebit ? "−" : "+"}${money(entry.amount)}` }),
      ),
    );
  });
}

async function loadRecent(accountId = state.selectedAccountId) {
  const container = $("#recent-activity");
  if (!accountId) {
    renderActivity(container, [], "Open an account to get started.");
    return;
  }
  const result = await api(`/accounts/${encodeURIComponent(accountId)}/transactions?limit=5`);
  renderActivity(container, result.transactions, "Add money from a partner bank to get started.");
}

async function loadActivity() {
  const select = $("#activity-account");
  const accountId = select.value || state.selectedAccountId;
  const container = $("#activity-list");
  if (!accountId) {
    renderActivity(container, [], "Open an account to see its activity.");
    return;
  }
  select.value = accountId;
  const result = await api(`/accounts/${encodeURIComponent(accountId)}/transactions?limit=50`);
  renderActivity(container, result.transactions, "Transactions on this account will show up here.");
}

/* ---------------------------------------------------------------- documents */

async function loadStatements() {
  const container = $("#statement-list");
  const select = $('#statement-form select[name="account_id"]');
  const accountId = select.value || state.selectedAccountId;
  window.clearInterval(state.statementTimer);
  if (!accountId) {
    container.replaceChildren(el("div", { className: "empty" }, el("span", { text: "Open an account to request statements." })));
    return;
  }
  const { statements } = await api(`/accounts/${encodeURIComponent(accountId)}/statements`);
  container.replaceChildren();
  if (!statements.length) {
    container.append(el("div", { className: "empty" }, el("strong", { text: "No statements yet" }), el("span", { text: "Request one above." })));
  }
  statements.forEach((statement) => {
    const previous = state.statementStatus.get(statement.job_id);
    if (previous === "QUEUED" && statement.status === "COMPLETED") notify.success("Your statement is ready to download.");
    if (previous === "QUEUED" && statement.status === "FAILED") notify.error("We couldn't create that statement. Please request it again.");
    state.statementStatus.set(statement.job_id, statement.status);
    const row = el(
      "div",
      { className: "list-item" },
      el(
        "span",
        { className: "row-copy" },
        el("strong", { text: statement.type === "MONTHLY" ? "Monthly statement" : "Statement" }),
        el("span", { text: `${statement.start_date || "All time"} → ${statement.end_date || "today"}` }),
      ),
      el("span", { className: `status-pill ${statement.status.toLowerCase()}`, text: statement.status }),
    );
    if (statement.status === "COMPLETED") {
      const download = el("button", { type: "button", className: "small-button", text: "Download" });
      download.addEventListener("click", () => downloadStatement(accountId, statement.job_id, download));
      row.append(download);
    }
    container.append(row);
  });
  if (statements.some((statement) => statement.status === "QUEUED")) {
    state.statementTimer = window.setInterval(() => loadStatements().catch(() => {}), 4000);
  }
}

async function downloadStatement(accountId, jobId, button) {
  button.disabled = true;
  try {
    const statement = await api(`/accounts/${encodeURIComponent(accountId)}/statements/${encodeURIComponent(jobId)}`);
    notify.success("Downloading your statement.");
    window.location.assign(statement.download_url);
  } catch (error) {
    notify.error(error.message);
  } finally {
    button.disabled = false;
  }
}

async function loadBanks() {
  if (state.banks.length) return;
  const result = await api("/external-banks");
  state.banks = result.banks;
  $("#bank-options").replaceChildren(...state.banks.map((bank) => new Option(bank.name, bank.code)));
}

async function refreshAlertDot() {
  const result = await api("/me/alerts");
  $("#alerts-dot").hidden = result.status === "ACTIVE";
  return result;
}

function renderAlertStatus(result) {
  const labels = { NONE: "Off", PENDING_CONFIRMATION: "Waiting for you to confirm the email", ACTIVE: `On · ${result.email}` };
  $("#alerts-status").textContent = labels[result.status] || result.status;
  $("#alerts-submit").hidden = result.status === "ACTIVE";
  $("#alerts-dot").hidden = result.status === "ACTIVE";
}

/* ---------------------------------------------------------------- forms */

function fieldLabel(input) {
  const label = input.closest("label");
  if (!label) return input.name;
  return [...label.childNodes].filter((node) => node.nodeType === Node.TEXT_NODE).map((node) => node.textContent.trim()).join(" ").trim() || input.name;
}

function validateForm(form) {
  form.querySelectorAll("[aria-invalid]").forEach((input) => input.removeAttribute("aria-invalid"));
  const invalid = [...form.elements].find((input) => input.willValidate && !input.closest("[hidden]") && !input.checkValidity());
  if (!invalid) return true;
  invalid.setAttribute("aria-invalid", "true");
  invalid.focus();
  const message = invalid.validity.valueMissing ? "is required" : invalid.validationMessage || "is not valid";
  notify.error(`${fieldLabel(invalid)}: ${message.charAt(0).toLowerCase()}${message.slice(1)}`);
  return false;
}

function invalidField(form, name, message) {
  const input = form.elements[name];
  if (input) {
    input.setAttribute("aria-invalid", "true");
    input.focus();
  }
  notify.error(message);
  return false;
}

async function submitForm(form, pendingMessage, handler) {
  if (!validateForm(form)) return;
  const button = form.querySelector('button[type="submit"]');
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "Just a moment…";
  const pending = pendingMessage ? notify.info(pendingMessage, { duration: 0 }) : null;
  try {
    const result = await handler(new FormData(form), form);
    if (result === false) pending?.dismiss();
    else if (pending && result) pending.update("success", result);
    else if (pending) pending.dismiss();
    else if (result) notify.success(result);
  } catch (error) {
    if (pending) pending.update("error", error.message || "Something went wrong.");
    else notify.error(error.message || "Something went wrong.");
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

function onSubmit(selector, pendingMessage, handler) {
  $(selector).addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(event.currentTarget, pendingMessage, handler);
  });
}

function setMoneyDirection(direction) {
  const deposit = direction === "deposit";
  $("#money-title").textContent = deposit ? "Add money" : "Withdraw money";
  $("#money-submit").textContent = deposit ? "Add money" : "Withdraw";
  $(`#money-form input[name="direction"][value="${direction}"]`).checked = true;
}

function wireEvents() {
  window.addEventListener("hashchange", showRoute);
  $$("[data-theme-toggle]").forEach((button) => button.addEventListener("click", toggleTheme));
  $$(".auth-tabs .segment").forEach((tab) => tab.addEventListener("click", () => showAuthPanel(tab.dataset.authTab)));

  onSubmit("#signin-form", "Signing you in…", async (data) => {
    const email = data.get("email").trim().toLowerCase();
    try {
      return await afterSignInStep(await signIn(email, data.get("password")));
    } catch (error) {
      if (error.type === "UserNotConfirmedException") {
        state.pendingSignUp = { email, password: data.get("password") };
        $("#confirm-email").textContent = email;
        await cognito("ResendConfirmationCode", { ClientId: CONFIG.clientId, Username: email });
        showAuthPanel("confirm-form");
        return "Confirm your email first. We sent you a new code.";
      }
      throw error;
    }
  });

  onSubmit("#signup-form", "Creating your login…", async (data) => {
    const email = data.get("email").trim().toLowerCase();
    const password = data.get("password");
    await cognito("SignUp", {
      ClientId: CONFIG.clientId,
      Username: email,
      Password: password,
      UserAttributes: [{ Name: "email", Value: email }],
    });
    state.pendingSignUp = { email, password };
    $("#confirm-email").textContent = email;
    showAuthPanel("confirm-form");
    return `We emailed a code to ${email}.`;
  });

  onSubmit("#confirm-form", "Verifying your email…", async (data, form) => {
    const { email, password } = state.pendingSignUp;
    await cognito("ConfirmSignUp", { ClientId: CONFIG.clientId, Username: email, ConfirmationCode: data.get("code").trim() });
    form.reset();
    const message = await afterSignInStep(await signIn(email, password));
    state.pendingSignUp = null;
    return `Email verified. ${message}`;
  });

  $("#resend-code").addEventListener("click", () => {
    const email = state.pendingSignUp?.email;
    if (!email) return;
    cognito("ResendConfirmationCode", { ClientId: CONFIG.clientId, Username: email })
      .then(() => notify.success("We sent a new code."))
      .catch((error) => notify.error(error.message));
  });

  onSubmit("#mfa-setup-form", "Checking your code…", async (data, form) => {
    const message = await afterSignInStep(await completeMfaSetup(data.get("code").trim()));
    form.reset();
    return `Authenticator app connected. ${message}`;
  });

  onSubmit("#mfa-form", "Checking your code…", async (data, form) => {
    const message = await afterSignInStep(await answerMfa(data.get("code").trim()));
    form.reset();
    return message;
  });

  $("#sign-out").addEventListener("click", signOut);

  $$("[data-open-modal]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.direction) setMoneyDirection(button.dataset.direction);
      $(`#${button.dataset.openModal}`).showModal();
      if (button.dataset.openModal === "alerts-modal") loadAlertStatus();
      if (button.dataset.openModal === "money-modal") updateAvailableHints();
    });
  });
  $$(".modal").forEach((dialog) => {
    const close = dialog.querySelector(".modal-close");
    if (close) close.addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog && dialog.id !== "profile-modal") dialog.close();
    });
  });
  $("#profile-modal").addEventListener("cancel", (event) => event.preventDefault());
  $("#step-up-modal").addEventListener("close", () => {
    if (state.stepUp) {
      state.stepUp.reject(new Error("Confirmation cancelled. Nothing was sent."));
      state.stepUp = null;
    }
  });

  onSubmit("#profile-form", "Saving your profile…", async (data, form) => {
    state.profile = await api("/me", { method: "POST", body: { full_name: data.get("full_name").trim() } });
    form.reset();
    $("#profile-modal").close();
    renderProfile();
    syncAccounts(state.profile.accounts);
    await afterProfile();
    return `Welcome, ${state.profile.full_name.split(" ")[0]}! Verify your identity, then open your first account.`;
  });

  onSubmit("#account-form", "Opening your account…", async (data, form) => {
    const type = data.get("account_type");
    if ((accountCounts()[type] || 0) >= ACCOUNT_LIMITS[type]) {
      return invalidField(form, "account_type", `You already have ${ACCOUNT_LIMITS[type]} ${typeLabel(type).toLowerCase()} accounts.`);
    }
    const account = await api("/accounts", {
      method: "POST",
      body: { account_type: type, nickname: data.get("nickname").trim() || null },
    });
    form.reset();
    $("#account-modal").close();
    await loadProfile();
    await selectAccount(account.account_id);
    return `Your ${typeLabel(type).toLowerCase()} account is open. Add money to get started.`;
  });

  onSubmit("#kyc-form", "Uploading your document securely…", async (data, form) => {
    const file = data.get("file");
    if (file && file.size > 10 * 1024 * 1024) return invalidField(form, "file", "That file is larger than 10 MB.");
    const body = new FormData();
    body.append("file", file);
    await api("/kyc/upload", { method: "POST", formData: body });
    form.reset();
    await loadProfile();
    return "Identity verified (simulated). You can now move money.";
  });

  $("#receiver-choice").addEventListener("change", (event) => {
    const other = event.target.value === OTHER_RECIPIENT;
    $("#receiver-other").hidden = !other;
    $("#receiver-other input").required = other;
  });
  $('#transfer-form select[name="sender_account_id"]').addEventListener("change", updateAvailableHints);
  $('#money-form select[name="account_id"]').addEventListener("change", updateAvailableHints);
  $("#activity-account").addEventListener("change", () => loadActivity().catch((error) => notify.error(error.message)));
  $('#statement-form select[name="account_id"]').addEventListener("change", () => loadStatements().catch((error) => notify.error(error.message)));

  onSubmit("#transfer-form", "Sending your transfer…", async (data, form) => {
    const choice = data.get("receiver_choice");
    const receiver = choice === OTHER_RECIPIENT ? data.get("receiver_account_id").trim().toLowerCase() : choice;
    const sender = data.get("sender_account_id");
    const amount = Number(data.get("amount"));
    if (receiver === sender) return invalidField(form, "receiver_choice", "Choose a different account to send to.");
    const available = availableBalance(sender);
    if (available !== null && amount > available) {
      return invalidField(form, "amount", `Insufficient funds: you have ${money(available)} available.`);
    }
    const result = await api("/transfers", {
      method: "POST",
      idempotencyKey: crypto.randomUUID(),
      body: { sender_account_id: sender, receiver_account_id: receiver, amount: data.get("amount") },
    });
    form.reset();
    $("#receiver-other").hidden = true;
    await Promise.all([selectAccount(sender), state.accounts.has(receiver) ? refreshBalance(receiver) : null]);
    const check = result.fraud_model.startsWith("sagemaker") ? "AI fraud check" : "Fraud check";
    const to = state.accounts.has(receiver) ? accountLabel(state.accounts.get(receiver)) : `account ${receiver.slice(0, 8)}`;
    return `Sent ${money(result.amount)} to ${to}. ${check} passed.`;
  });

  $$('#money-form input[name="direction"]').forEach((radio) => radio.addEventListener("change", () => setMoneyDirection(radio.value)));

  onSubmit("#money-form", "Contacting the partner bank…", async (data, form) => {
    const accountId = data.get("account_id");
    const deposit = data.get("direction") === "deposit";
    const amount = Number(data.get("amount"));
    const available = availableBalance(accountId);
    if (!deposit && available !== null && amount > available) {
      return invalidField(form, "amount", `You can't withdraw more than your available balance of ${money(available)}.`);
    }
    const result = await api(`/accounts/${encodeURIComponent(accountId)}/${deposit ? "deposits" : "withdrawals"}`, {
      method: "POST",
      idempotencyKey: crypto.randomUUID(),
      body: { amount: data.get("amount"), bank_code: data.get("bank_code"), external_account: data.get("external_account") },
    });
    form.reset();
    setMoneyDirection("deposit");
    $("#money-modal").close();
    await selectAccount(accountId);
    return `${deposit ? "Added" : "Withdrew"} ${money(result.amount)} · reference ${result.external_reference}.`;
  });

  onSubmit("#statement-form", "Requesting your statement…", async (data, form) => {
    const start = data.get("start_date");
    const end = data.get("end_date");
    if (start && end && end < start) return invalidField(form, "end_date", "The end date must be on or after the start date.");
    await api("/statements/request", {
      method: "POST",
      body: { account_id: data.get("account_id"), start_date: start || null, end_date: end || null },
    });
    form.elements.start_date.value = "";
    form.elements.end_date.value = "";
    await loadStatements();
    return "Statement requested. We'll let you know when it's ready.";
  });

  onSubmit("#step-up-form", "Confirming it's you…", async (data, form) => {
    await stepUp(data.get("password"), data.get("code").trim());
    form.reset();
    const pending = state.stepUp;
    state.stepUp = null;
    $("#step-up-modal").close();
    pending.resolve();
    return "Thanks, you're confirmed.";
  });

  onSubmit("#alerts-form", "Setting up email alerts…", async () => {
    const result = await api("/me/alerts", { method: "POST" });
    renderAlertStatus(result);
    return `Check ${result.email} and click the confirmation link from AWS Notifications.`;
  });

  $("#refresh-balance").addEventListener("click", () =>
    refreshBalance().then(() => notify.success("Balance updated.", { duration: 2000 })).catch((error) => notify.error(error.message)),
  );
  $("#refresh-recent").addEventListener("click", () => loadRecent().catch((error) => notify.error(error.message)));
  $("#refresh-activity").addEventListener("click", () => loadActivity().catch((error) => notify.error(error.message)));
  $("#refresh-statements").addEventListener("click", () => loadStatements().catch((error) => notify.error(error.message)));
  $("#copy-account").addEventListener("click", () => {
    if (!state.selectedAccountId) return notify.error("Open an account first.");
    navigator.clipboard
      .writeText(state.selectedAccountId)
      .then(() => notify.success("Account number copied. Share it to receive transfers."))
      .catch(() => notify.info(`Your account number: ${state.selectedAccountId}`, { duration: 10000 }));
  });
}

function loadAlertStatus() {
  $("#alerts-status").textContent = "Checking…";
  api("/me/alerts").then(renderAlertStatus).catch((error) => notify.error(error.message));
}

function start() {
  wireEvents();
  const today = new Intl.DateTimeFormat("en-SG", { weekday: "long", day: "numeric", month: "long" });
  $("#today-label").textContent = today.format(new Date()).toUpperCase();
  if (!CONFIG.userPoolId || !CONFIG.clientId) {
    notify.error("Sign-in isn't configured. Set userPoolId and clientId in assets/config.js.");
  }
  state.session = loadSession();
  if (state.session) enterApp();
  else showAuthPanel("signin-form");
}

start();
