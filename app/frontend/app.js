const API_BASE = window.FACAI_API_BASE || "http://localhost:8000";
const accounts = new Map();
let selectedAccountId = "";
let toastTimer;

const money = (value) => new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
}).format(Number(value));

function showToast(message, isError = false) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 4200);
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, options);
  } catch {
    throw new Error("Could not reach the FaCai Bank API. Check that the backend is running.");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || `Request failed (${response.status})`;
    throw new Error(Array.isArray(detail) ? detail.map((item) => item.msg).join(", ") : detail);
  }
  return payload;
}

function updateAccountSelectors() {
  document.querySelectorAll(".account-options").forEach((select) => {
    const selected = select.value;
    select.replaceChildren(new Option("Select an account", ""));
    accounts.forEach((account) => {
      const option = new Option(
        `${account.full_name} · ${account.account_id.slice(0, 8)}`,
        account.account_id,
      );
      select.add(option);
    });
    if (accounts.has(selected)) select.value = selected;
  });
}

function renderAccounts() {
  const container = document.querySelector("#account-list");
  container.replaceChildren();
  if (!accounts.size) {
    const empty = document.createElement("div");
    empty.className = "account-empty";
    empty.textContent = "Your accounts will appear here.";
    container.append(empty);
  }

  accounts.forEach((account) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `account-item${account.account_id === selectedAccountId ? " selected" : ""}`;
    item.innerHTML = `
      <span class="account-item-icon">F</span>
      <span class="account-item-copy"><strong>${escapeHtml(account.full_name)}</strong>
      <span>${escapeHtml(account.account_id)}</span></span>
      <span class="account-item-balance">${money(account.balance || 0)}</span>`;
    item.addEventListener("click", () => selectAccount(account.account_id));
    container.append(item);
  });
  updateAccountSelectors();
}

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = value;
  return element.innerHTML;
}

async function refreshBalance(accountId = selectedAccountId) {
  if (!accountId || !accounts.has(accountId)) return;
  const result = await api(`/accounts/${encodeURIComponent(accountId)}/balance`);
  const account = accounts.get(accountId);
  account.balance = result.balance;
  document.querySelector("#balance-amount").textContent = money(result.balance);
  document.querySelector("#account-summary").textContent = `Everyday account · ${accountId.slice(0, 8)}`;
  renderAccounts();
}

async function refreshTransactions(accountId = selectedAccountId) {
  const list = document.querySelector("#activity-list");
  list.replaceChildren();
  if (!accountId) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = '<span class="empty-icon">◷</span><strong>Your story starts here</strong><p>Create an account to see your transactions.</p>';
    list.append(empty);
    return;
  }
  const result = await api(`/accounts/${encodeURIComponent(accountId)}/transactions?limit=20`);
  if (!result.transactions.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = '<span class="empty-icon">◷</span><strong>All quiet for now</strong><p>Your transactions will show up here.</p>';
    list.append(empty);
    return;
  }
  result.transactions.forEach((transaction) => {
    const row = document.createElement("div");
    row.className = "activity-row";
    const isDebit = transaction.entry_type === "DEBIT";
    const date = new Date(transaction.created_at);
    row.innerHTML = `
      <span class="activity-symbol ${isDebit ? "debit" : ""}">${isDebit ? "↗" : "↙"}</span>
      <span class="activity-description"><strong>${isDebit ? "Transfer sent" : "Transfer received"}</strong>
      <span>${date.toLocaleString()} · ${escapeHtml(transaction.transfer_id || "")}</span></span>
      <span class="activity-amount ${isDebit ? "debit" : ""}">${isDebit ? "−" : "+"}${money(transaction.amount)}</span>`;
    list.append(row);
  });
}

async function selectAccount(accountId) {
  selectedAccountId = accountId;
  document.querySelectorAll(".account-options").forEach((select) => {
    if (select.querySelector(`option[value="${CSS.escape(accountId)}"]`)) select.value = accountId;
  });
  renderAccounts();
  try {
    await Promise.all([refreshBalance(accountId), refreshTransactions(accountId)]);
  } catch (error) {
    showToast(error.message, true);
  }
}

function openModal(id) {
  document.querySelector(`#${id}`).showModal();
}

function closeModal(dialog) {
  dialog.close();
}

function formObject(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function submitForm(form, callback) {
  const button = form.querySelector('button[type="submit"]');
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "Just a moment…";
  try {
    await callback(new FormData(form));
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

document.querySelectorAll("[data-open-modal]").forEach((button) => {
  button.addEventListener("click", () => openModal(button.dataset.openModal));
});
document.querySelectorAll(".modal").forEach((dialog) => {
  dialog.querySelector(".modal-close").addEventListener("click", () => closeModal(dialog));
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeModal(dialog);
  });
});

document.querySelector("#account-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submitForm(form, async (data) => {
    const account = await api("/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(data.entries())),
    });
    accounts.set(account.account_id, {
      account_id: account.account_id,
      full_name: data.get("full_name"),
      balance: account.balance,
    });
    selectedAccountId = account.account_id;
    renderAccounts();
    await selectAccount(account.account_id);
    form.reset();
    closeModal(document.querySelector("#account-modal"));
    showToast("Your new account is ready with a $1,000.00 starting balance.");
  });
});

document.querySelector("#transfer-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submitForm(form, async (data) => {
    const result = await api("/transfers", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify({
        sender_account_id: data.get("sender_account_id"),
        receiver_account_id: data.get("receiver_account_id").trim(),
        amount: data.get("amount"),
      }),
    });
    form.reset();
    await selectAccount(result.transfer_id ? data.get("sender_account_id") : selectedAccountId);
    showToast(`Transfer sent: ${money(result.amount)} · Fraud check ${Math.round(result.fraud_score * 100)}%.`);
  });
});

document.querySelector("#kyc-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submitForm(form, async (data) => {
    const accountId = data.get("account_id");
    const body = new FormData();
    body.append("file", data.get("file"));
    const result = await api(`/kyc/upload?account_id=${encodeURIComponent(accountId)}`, {
      method: "POST",
      body,
    });
    form.reset();
    closeModal(document.querySelector("#kyc-modal"));
    showToast(`Identity check complete: ${result.kyc_status}.`);
  });
});

document.querySelector("#statement-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submitForm(form, async (data) => {
    const values = formObject(form);
    const result = await api("/statements/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account_id: values.account_id,
        start_date: values.start_date || null,
        end_date: values.end_date || null,
      }),
    });
    form.reset();
    closeModal(document.querySelector("#statement-modal"));
    showToast(`Statement request queued · Job ${result.job_id.slice(0, 8)}.`);
  });
});

document.querySelector("#refresh-balance").addEventListener("click", () => {
  refreshBalance().catch((error) => showToast(error.message, true));
});
document.querySelector("#refresh-transactions").addEventListener("click", () => {
  refreshTransactions().catch((error) => showToast(error.message, true));
});

const today = new Intl.DateTimeFormat("en-US", { weekday: "long", month: "long", day: "numeric" });
document.querySelector("#today-label").textContent = today.format(new Date()).toUpperCase();
