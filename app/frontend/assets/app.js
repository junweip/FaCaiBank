"use strict";

const CONFIG = Object.assign(
  { apiBase: "/api", region: "ap-southeast-1", userPoolId: "", clientId: "", version: "dev" },
  window.FACAI_CONFIG || {},
);
const COGNITO_URL = `https://cognito-idp.${CONFIG.region}.amazonaws.com/`;
const SESSION_KEY = "facai.session";
const OTHER_RECIPIENT = "__other__";

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
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);
let toastTimer;

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

function showToast(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 5200);
}

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
    signOutLocally("Your session ended. Please sign in again.");
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
  if (!detail) return `Request failed (${status})`;
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
    signOutLocally("Your session ended. Please sign in again.");
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
  return new Promise((resolve, reject) => {
    state.stepUp = { resolve, reject };
    $("#step-up-modal").showModal();
  });
}

/* ---------------------------------------------------------------- views */

function showAuthPanel(id) {
  $$(".auth-panel").forEach((panel) => {
    panel.hidden = panel.id !== id;
  });
  $$(".auth-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.authTab === id));
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
  if (step === "mfa") showAuthPanel("mfa-form");
  else if (step === "mfa_setup") showMfaSetup();
  else await enterApp();
}

function signOutLocally(message) {
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
  if (message) showToast(message, true);
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
  showToast("You're signed out.");
}

async function enterApp() {
  document.body.classList.remove("signed-out");
  $("#app-view").hidden = false;
  $("#version-label").textContent = `Prototype · ${CONFIG.version}`;
  try {
    if (await loadProfile()) await afterProfile();
  } catch (error) {
    showToast(error.message, true);
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
  if (state.selectedAccountId) await selectAccount(state.selectedAccountId);
  await refreshAllBalances();
}

function renderProfile() {
  const profile = state.profile;
  const initials = profile.full_name.split(/\s+/).map((part) => part[0]).slice(0, 2).join("").toUpperCase();
  $("#profile-initials").textContent = initials || "FC";
  $("#profile-name").textContent = profile.full_name;
  $("#profile-email").textContent = profile.email;
  const verified = profile.kyc_status === "VERIFIED_MOCK";
  $("#kyc-label").textContent = verified ? "Identity verified" : "Identity check pending";
  $("#kyc-banner").hidden = verified;
  $("#welcome-copy").textContent = `Welcome back, ${profile.full_name.split(" ")[0]}. A little more clarity for everything you’re building.`;
}

function accountLabel(account) {
  const kind = account.account_type === "SAVINGS" ? "Savings" : "Everyday";
  return `${account.nickname || kind} · ${account.account_id.slice(0, 8)}`;
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
  const container = $("#account-list");
  container.replaceChildren();
  if (!state.accounts.size) container.append(el("div", { className: "account-empty", text: "Your accounts will appear here." }));
  state.accounts.forEach((account) => {
    const item = el(
      "button",
      { type: "button", className: `account-item${account.account_id === state.selectedAccountId ? " selected" : ""}` },
      el("span", { className: "account-item-icon", text: account.account_type === "SAVINGS" ? "S" : "F" }),
      el(
        "span",
        { className: "account-item-copy" },
        el("strong", { text: account.nickname || (account.account_type === "SAVINGS" ? "Savings" : "Everyday") }),
        el("span", { text: account.account_id }),
      ),
      el("span", { className: "account-item-balance", text: account.balance === undefined ? "…" : money(account.balance) }),
    );
    item.addEventListener("click", () => selectAccount(account.account_id));
    container.append(item);
  });
  updateAccountSelectors();
}

function updateAccountSelectors() {
  $$(".account-options").forEach((select) => {
    const selected = select.value;
    const includeOther = select.dataset.includeOther === "true";
    select.replaceChildren(new Option(includeOther ? "Select a recipient" : "Select an account", ""));
    const group = includeOther ? el("optgroup", { label: "My accounts" }) : select;
    state.accounts.forEach((account) => group.append(new Option(accountLabel(account), account.account_id)));
    if (includeOther) {
      select.append(group, new Option("Another FaCaiBank account…", OTHER_RECIPIENT));
    }
    if ([...select.options].some((option) => option.value === selected)) select.value = selected;
    else if (!includeOther && state.selectedAccountId) select.value = state.selectedAccountId;
  });
}

async function selectAccount(accountId) {
  if (!accountId) return;
  state.selectedAccountId = accountId;
  renderAccounts();
  try {
    await Promise.all([refreshBalance(accountId), refreshTransactions(accountId), refreshStatements(accountId)]);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function refreshBalance(accountId = state.selectedAccountId) {
  if (!accountId || !state.accounts.has(accountId)) return;
  const result = await api(`/accounts/${encodeURIComponent(accountId)}/balance`);
  state.accounts.get(accountId).balance = result.balance;
  if (accountId === state.selectedAccountId) {
    $("#balance-amount").textContent = money(result.balance);
    $("#account-summary").textContent = accountLabel(state.accounts.get(accountId));
  }
  renderAccounts();
}

async function refreshAllBalances() {
  await Promise.all([...state.accounts.keys()].map((accountId) => refreshBalance(accountId).catch(() => {})));
}

function describeEntry(entry) {
  const bank = String(entry.counterparty_account_id || "").replace("EXTERNAL:", "");
  const descriptions = {
    "TRANSFER:DEBIT": "Transfer sent",
    "TRANSFER:CREDIT": "Transfer received",
    "DEPOSIT:CREDIT": `Deposit from ${bank}`,
    "WITHDRAWAL:DEBIT": `Withdrawal to ${bank}`,
    "REVERSAL:CREDIT": `Withdrawal returned by ${bank}`,
  };
  return descriptions[`${entry.transaction_type}:${entry.entry_type}`] || entry.transaction_type;
}

async function refreshTransactions(accountId = state.selectedAccountId) {
  const list = $("#activity-list");
  if (!accountId) return;
  const result = await api(`/accounts/${encodeURIComponent(accountId)}/transactions?limit=20`);
  list.replaceChildren();
  if (!result.transactions.length) {
    list.append(
      el(
        "div",
        { className: "empty-state" },
        el("span", { className: "empty-icon", text: "◷" }),
        el("strong", { text: "All quiet for now" }),
        el("p", { text: "Add money from a partner bank to get started." }),
      ),
    );
    return;
  }
  result.transactions.forEach((entry) => {
    const isDebit = entry.entry_type === "DEBIT";
    const status = { PENDING_EXTERNAL: "Pending", REVERSED: "Reversed" }[entry.status];
    const title = el("strong", { text: describeEntry(entry) });
    if (status) title.append(el("span", { className: "activity-status", text: status }));
    list.append(
      el(
        "div",
        { className: "activity-row" },
        el("span", { className: `activity-symbol ${isDebit ? "debit" : ""}`, text: isDebit ? "↗" : "↙" }),
        el(
          "span",
          { className: "activity-description" },
          title,
          el("span", { text: `${new Date(entry.created_at).toLocaleString("en-SG")} · ${entry.transaction_id.slice(0, 8)}` }),
        ),
        el("span", { className: `activity-amount ${isDebit ? "debit" : ""}`, text: `${isDebit ? "−" : "+"}${money(entry.amount)}` }),
      ),
    );
  });
}

async function refreshStatements(accountId = state.selectedAccountId) {
  const container = $("#statement-list");
  if (!accountId) return;
  const { statements } = await api(`/accounts/${encodeURIComponent(accountId)}/statements`);
  container.replaceChildren();
  if (!statements.length) {
    container.append(el("div", { className: "account-empty", text: "Statements you request will appear here." }));
  }
  statements.forEach((statement) => {
    const period = `${statement.start_date || "All time"} → ${statement.end_date || "today"}`;
    const status = statement.status.toLowerCase();
    const row = el(
      "div",
      { className: "statement-item" },
      el(
        "span",
        { className: "account-item-copy" },
        el("strong", { text: statement.type === "MONTHLY" ? "Monthly statement" : "Statement" }),
        el("span", { text: period }),
      ),
      el("span", { className: `status-pill ${status}`, text: statement.status }),
    );
    if (statement.status === "COMPLETED") {
      const download = el("button", { type: "button", className: "small-button", text: "Download" });
      download.addEventListener("click", () => downloadStatement(accountId, statement.job_id, download));
      row.append(download);
    }
    container.append(row);
  });
  window.clearInterval(state.statementTimer);
  if (statements.some((statement) => statement.status === "QUEUED")) {
    state.statementTimer = window.setInterval(() => refreshStatements(accountId).catch(() => {}), 4000);
  }
}

async function downloadStatement(accountId, jobId, button) {
  button.disabled = true;
  try {
    const statement = await api(`/accounts/${encodeURIComponent(accountId)}/statements/${encodeURIComponent(jobId)}`);
    window.location.assign(statement.download_url);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function loadBanks() {
  if (state.banks.length) return;
  const result = await api("/external-banks");
  state.banks = result.banks;
  const select = $("#bank-options");
  select.replaceChildren(...state.banks.map((bank) => new Option(bank.name, bank.code)));
}

async function refreshAlertDot() {
  const result = await api("/me/alerts");
  $("#alerts-dot").hidden = result.status === "ACTIVE";
  return result;
}

/* ---------------------------------------------------------------- forms */

async function submitForm(form, callback) {
  const button = form.querySelector('button[type="submit"]');
  const originalText = button.innerHTML;
  button.disabled = true;
  button.textContent = "Just a moment…";
  try {
    await callback(new FormData(form));
  } catch (error) {
    if (error.message) showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.innerHTML = originalText;
  }
}

function on(selector, handler) {
  $(selector).addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(event.currentTarget, (data) => handler(data, event.currentTarget));
  });
}

function setMoneyDirection(direction) {
  const deposit = direction === "deposit";
  $("#money-title").textContent = deposit ? "Add money" : "Withdraw money";
  $(`#money-form input[name="direction"][value="${direction}"]`).checked = true;
}

function wireEvents() {
  $$(".auth-tab").forEach((tab) => tab.addEventListener("click", () => showAuthPanel(tab.dataset.authTab)));

  on("#signin-form", async (data) => {
    const email = data.get("email").trim().toLowerCase();
    try {
      await afterSignInStep(await signIn(email, data.get("password")));
    } catch (error) {
      if (error.type === "UserNotConfirmedException") {
        state.pendingSignUp = { email, password: data.get("password") };
        $("#confirm-email").textContent = email;
        await cognito("ResendConfirmationCode", { ClientId: CONFIG.clientId, Username: email });
        showAuthPanel("confirm-form");
        return;
      }
      throw error;
    }
  });

  on("#signup-form", async (data) => {
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
  });

  on("#confirm-form", async (data, form) => {
    const { email, password } = state.pendingSignUp;
    await cognito("ConfirmSignUp", { ClientId: CONFIG.clientId, Username: email, ConfirmationCode: data.get("code").trim() });
    form.reset();
    showToast("Email verified. Next, set up your authenticator app.");
    await afterSignInStep(await signIn(email, password));
    state.pendingSignUp = null;
  });

  $("#resend-code").addEventListener("click", () => {
    const email = state.pendingSignUp?.email;
    if (!email) return;
    cognito("ResendConfirmationCode", { ClientId: CONFIG.clientId, Username: email })
      .then(() => showToast("We sent a new code."))
      .catch((error) => showToast(error.message, true));
  });

  on("#mfa-setup-form", async (data, form) => {
    await afterSignInStep(await completeMfaSetup(data.get("code").trim()));
    form.reset();
  });

  on("#mfa-form", async (data, form) => {
    await afterSignInStep(await answerMfa(data.get("code").trim()));
    form.reset();
  });

  $("#sign-out").addEventListener("click", signOut);

  $$("[data-open-modal]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.direction) setMoneyDirection(button.dataset.direction);
      $(`#${button.dataset.openModal}`).showModal();
      if (button.dataset.openModal === "alerts-modal") loadAlertStatus();
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

  on("#profile-form", async (data, form) => {
    state.profile = await api("/me", { method: "POST", body: { full_name: data.get("full_name") } });
    form.reset();
    $("#profile-modal").close();
    renderProfile();
    syncAccounts(state.profile.accounts);
    await afterProfile();
    showToast("Welcome to FaCai Bank! Verify your identity, then open your first account.");
  });

  on("#account-form", async (data, form) => {
    const account = await api("/accounts", {
      method: "POST",
      body: { account_type: data.get("account_type"), nickname: data.get("nickname") || null },
    });
    form.reset();
    $("#account-modal").close();
    await loadProfile();
    await selectAccount(account.account_id);
    showToast("Your new account is open. Add money from a partner bank to get started.");
  });

  on("#kyc-form", async (data, form) => {
    const body = new FormData();
    body.append("file", data.get("file"));
    await api("/kyc/upload", { method: "POST", formData: body });
    form.reset();
    $("#kyc-modal").close();
    await loadProfile();
    showToast("Identity verified (simulated). You can now move money.");
  });

  $("#receiver-choice").addEventListener("change", (event) => {
    const other = event.target.value === OTHER_RECIPIENT;
    $("#receiver-other").hidden = !other;
    $("#receiver-other input").required = other;
  });

  on("#transfer-form", async (data, form) => {
    const choice = data.get("receiver_choice");
    const receiver = choice === OTHER_RECIPIENT ? data.get("receiver_account_id").trim() : choice;
    const sender = data.get("sender_account_id");
    const result = await api("/transfers", {
      method: "POST",
      idempotencyKey: crypto.randomUUID(),
      body: { sender_account_id: sender, receiver_account_id: receiver, amount: data.get("amount") },
    });
    form.reset();
    $("#receiver-other").hidden = true;
    await Promise.all([selectAccount(sender), state.accounts.has(receiver) ? refreshBalance(receiver) : null]);
    const check = result.fraud_model.startsWith("sagemaker") ? "AI fraud check" : "Fraud check";
    showToast(`Sent ${money(result.amount)}. ${check} passed (risk ${Math.round(result.fraud_score * 100)}%).`);
  });

  $$('#money-form input[name="direction"]').forEach((radio) =>
    radio.addEventListener("change", () => setMoneyDirection(radio.value)),
  );

  on("#money-form", async (data, form) => {
    const accountId = data.get("account_id");
    const deposit = data.get("direction") === "deposit";
    const result = await api(`/accounts/${encodeURIComponent(accountId)}/${deposit ? "deposits" : "withdrawals"}`, {
      method: "POST",
      idempotencyKey: crypto.randomUUID(),
      body: { amount: data.get("amount"), bank_code: data.get("bank_code"), external_account: data.get("external_account") },
    });
    form.reset();
    setMoneyDirection("deposit");
    $("#money-modal").close();
    await selectAccount(accountId);
    showToast(`${deposit ? "Added" : "Withdrew"} ${money(result.amount)} · ref ${result.external_reference}`);
  });

  on("#statement-form", async (data, form) => {
    const accountId = data.get("account_id");
    await api("/statements/request", {
      method: "POST",
      body: { account_id: accountId, start_date: data.get("start_date") || null, end_date: data.get("end_date") || null },
    });
    form.reset();
    $("#statement-modal").close();
    await selectAccount(accountId);
    showToast("Statement requested. It will appear under Statements when it's ready.");
  });

  on("#step-up-form", async (data, form) => {
    await stepUp(data.get("password"), data.get("code").trim());
    form.reset();
    const pending = state.stepUp;
    state.stepUp = null;
    $("#step-up-modal").close();
    pending.resolve();
  });

  on("#alerts-form", async () => {
    const result = await api("/me/alerts", { method: "POST" });
    renderAlertStatus(result);
    showToast(`Check ${result.email} and click the confirmation link from AWS Notifications.`);
  });

  $("#refresh-balance").addEventListener("click", () => refreshBalance().catch((error) => showToast(error.message, true)));
  $("#refresh-transactions").addEventListener("click", () => refreshTransactions().catch((error) => showToast(error.message, true)));
  $("#refresh-statements").addEventListener("click", () => refreshStatements().catch((error) => showToast(error.message, true)));
  $("#copy-account").addEventListener("click", () => {
    if (!state.selectedAccountId) return;
    navigator.clipboard
      .writeText(state.selectedAccountId)
      .then(() => showToast("Account number copied. Share it to receive transfers."))
      .catch(() => showToast(state.selectedAccountId));
  });
}

function renderAlertStatus(result) {
  const labels = { NONE: "Off", PENDING_CONFIRMATION: "Waiting for you to confirm the email", ACTIVE: `On · ${result.email}` };
  $("#alerts-status").textContent = labels[result.status] || result.status;
  $("#alerts-submit").hidden = result.status === "ACTIVE";
  $("#alerts-dot").hidden = result.status === "ACTIVE";
}

function loadAlertStatus() {
  $("#alerts-status").textContent = "Checking…";
  api("/me/alerts").then(renderAlertStatus).catch((error) => showToast(error.message, true));
}

function start() {
  wireEvents();
  const today = new Intl.DateTimeFormat("en-SG", { weekday: "long", month: "long", day: "numeric" });
  $("#today-label").textContent = today.format(new Date()).toUpperCase();
  if (!CONFIG.userPoolId || !CONFIG.clientId) {
    showToast("Sign-in isn't configured. Set userPoolId and clientId in assets/config.js.", true);
  }
  state.session = loadSession();
  if (state.session) enterApp();
  else showAuthPanel("signin-form");
}

start();
