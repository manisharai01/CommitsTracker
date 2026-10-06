// Contribution reports web UI. Vanilla JS, no build step; served by
// github_contrib/webapp.py. Tokens live only in the form fields: they are
// sent with a job request and never written to browser storage.

const LOGIN_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/;
const EMAIL_RE = /^[^@\s,]+@[^@\s,]+$/;
const TOKEN_RE = /^[A-Za-z0-9_]{20,255}$/;
const FORM_KEY = "commitstracker.form.v1";
const THEME_KEY = "commitstracker.theme";
const ACTIVE = new Set(["queued", "running"]);
const MAX_ACCOUNTS = 20;
const POLL_ACTIVE_MS = 1200;
const POLL_IDLE_MS = 30000;
const NEW_TOKEN_URL =
  "https://github.com/settings/tokens/new?scopes=repo,read:org&description=CommitsTracker%20report";
const BASE_TITLE = document.title;

// Form switch id -> [option key, whether the switch reads as the opposite].
const TOGGLES = {
  "opt-own": ["exclude_own_repos", true],
  "opt-branches": ["default_branch_only", true],
  "opt-forks": ["skip_forks", true],
  "opt-stats": ["commit_stats", false],
  "opt-prs": ["pull_requests", false],
};
const FLAG_DEFAULTS = {
  exclude_own_repos: false,
  default_branch_only: false,
  skip_forks: false,
  commit_stats: true,
  pull_requests: true,
};
const LISTS = { "opt-repos": "extra_repos", "opt-orgs": "extra_orgs", "opt-owners": "exclude_owners" };

const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff:
    '<path d="M3 3l18 18"/><path d="M10.6 5.1A10.4 10.4 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.2 4.2M6.6 6.6C3.8 8.4 2 12 2 12s3.5 7 10 7c1.9 0 3.6-.6 5-1.4"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  sheet: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 10h16M10 4v16"/>',
  log: '<path d="M5 6h14M5 12h14M5 18h9"/>',
  redo: '<path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v4h-4"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  chevron: '<path d="M6 9l6 6 6-6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  alert:
    '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
};

const STATUS_LABELS = {
  queued: "Queued",
  running: "Running",
  done: "Ready",
  failed: "Failed",
  cancelled: "Cancelled",
};

const state = {
  config: {
    version: "",
    env_logins: [],
    has_default_token: false,
    defaults: {},
    pdf_browser: true,
    output_dir: "",
  },
  jobs: [],
  cards: new Map(), // job id -> { el, body, log, html }
  logs: new Map(), // job id -> { next, busy } for open log panels
  pollTimer: 0,
  unseen: 0,
};

// ---------- helpers ----------

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
const splitList = (text) => text.split(/[\s,]+/).filter(Boolean);
const envSuffix = (login) => login.replace(/[^A-Za-z0-9]/g, "_").toUpperCase();

function debounce(fn, ms) {
  let timer = 0;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Private window or blocked storage: the page works without it.
  }
}

function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICONS[name]}</svg>`;
}

function hydrateIcons(root) {
  for (const el of $$("[data-icon]", root)) el.outerHTML = icon(el.dataset.icon);
}

async function api(path, { method = "GET", body } = {}) {
  const init = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function relativeTime(iso) {
  const date = new Date(iso);
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (seconds < 45) return "now";
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  const options = { month: "short", day: "numeric" };
  if (date.getFullYear() !== new Date().getFullYear()) options.year = "numeric";
  return date.toLocaleDateString(undefined, options);
}

const fullDate = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
const shortDate = (iso) =>
  new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

let toastTimer = 0;
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 3500);
}

// ---------- theme ----------

function currentTheme() {
  return document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

function updateThemeButton() {
  const dark = currentTheme() === "dark";
  const button = $("#theme-toggle");
  button.innerHTML = icon(dark ? "sun" : "moon");
  button.setAttribute("aria-label", dark ? "Use light theme" : "Use dark theme");
}

function initTheme() {
  updateThemeButton();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", updateThemeButton);
  $("#theme-toggle").addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    storageSet(THEME_KEY, next);
    updateThemeButton();
  });
}

// ---------- avatars ----------

const avatarUrl = (login) => `https://github.com/${encodeURIComponent(login)}.png?size=96`;

function avatarHTML(login, extra = 0) {
  const more = extra > 0 ? `<span class="avatar-more">+${extra}</span>` : "";
  return `<span class="avatar" data-initial="${esc(login.charAt(0).toUpperCase() || "?")}">` +
    `<img src="${esc(avatarUrl(login))}" alt="" loading="lazy">${more}</span>`;
}

// Images reveal themselves once loaded and hide again (showing the initial) on failure.
document.addEventListener("load", (event) => {
  if (event.target instanceof HTMLImageElement && event.target.closest(".avatar")) event.target.hidden = false;
}, true);
document.addEventListener("error", (event) => {
  if (event.target instanceof HTMLImageElement && event.target.closest(".avatar")) event.target.hidden = true;
}, true);

// ---------- accounts ----------

let rowSeq = 0;
const loginOf = (row) => $("[name=login]", row).value.trim().replace(/^@/, "");

function envTokenName(login) {
  if (!login) return null;
  const suffix = envSuffix(login);
  if (state.config.env_logins.some((envLogin) => envSuffix(envLogin) === suffix)) return `GITHUB_TOKEN_${suffix}`;
  return state.config.has_default_token ? "GITHUB_TOKEN" : null;
}

function readAccounts() {
  return $$("#accounts .account").map((row) => ({
    row,
    login: loginOf(row),
    token: $("[name=token]", row).value.trim(),
    emails: $("[name=emails]", row).value.trim(),
  }));
}

function addAccount({ login = "", emails = "", token = "" } = {}) {
  const fragment = $("#account-template").content.cloneNode(true);
  hydrateIcons(fragment);
  const row = fragment.querySelector(".account");
  const seq = ++rowSeq;
  for (const input of $$("input", row)) {
    input.id = `${input.name}-${seq}`;
    $(`label[data-for="${input.name}"]`, row).htmlFor = input.id;
  }
  const inputs = { login: $("[name=login]", row), token: $("[name=token]", row), emails: $("[name=emails]", row) };
  inputs.login.value = login;
  inputs.token.value = token;
  inputs.emails.value = emails;

  const refreshAvatar = debounce(() => updateAvatar(row), 350);
  inputs.login.addEventListener("input", () => {
    refreshAvatar();
    updateTokenHint(row);
    clearRowError(row);
    updateSubmit();
  });
  inputs.token.addEventListener("input", () => {
    updateTokenHint(row);
    clearRowError(row);
  });
  inputs.emails.addEventListener("input", () => clearRowError(row));
  $("[data-role=reveal]", row).addEventListener("click", (event) => toggleReveal(row, event.currentTarget));
  $("[data-role=remove]", row).addEventListener("click", () => {
    row.remove();
    afterAccountsChange();
    $("#accounts .account:last-child [name=login]")?.focus();
  });

  $("#accounts").append(row);
  updateAvatar(row);
  updateTokenHint(row);
  afterAccountsChange();
  return row;
}

function afterAccountsChange() {
  const count = $$("#accounts .account").length;
  $("#accounts").classList.toggle("single", count === 1);
  $("#mode").hidden = count < 2;
  $("#add-account").hidden = count >= MAX_ACCOUNTS;
  updateSubmit();
  saveFormSoon();
}

function updateAvatar(row) {
  const login = loginOf(row);
  const avatar = $(".avatar", row);
  const img = $("img", avatar);
  avatar.dataset.initial = login ? login.charAt(0).toUpperCase() : "?";
  const url = LOGIN_RE.test(login) ? avatarUrl(login) : "";
  if ((img.getAttribute("src") || "") === url) return;
  img.hidden = true;
  if (url) img.src = url;
  else img.removeAttribute("src");
}

function updateTokenHint(row) {
  const token = $("[name=token]", row).value.trim();
  const envName = envTokenName(loginOf(row));
  const hint = $(".token-hint", row);
  hint.className = "hint token-hint";
  if (token.startsWith("github_pat_")) {
    hint.classList.add("hint-warning");
    hint.innerHTML = `${icon("alert")}<span>Fine-grained tokens can't see repos owned by other accounts or ` +
      `organizations. Use a classic token (ghp_…) with <b>repo</b> and <b>read:org</b>.</span>`;
  } else if (!token && envName) {
    hint.classList.add("hint-success");
    hint.innerHTML = `${icon("check")}<span>Leave blank to use <code>${esc(envName)}</code> from .env.</span>`;
  } else {
    hint.innerHTML = `<span>Classic token with <b>repo</b> and <b>read:org</b> scopes. ` +
      `<a href="${NEW_TOKEN_URL}" target="_blank" rel="noopener">Create one</a></span>`;
  }
}

function toggleReveal(row, button) {
  const input = $("[name=token]", row);
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  button.setAttribute("aria-pressed", String(show));
  button.setAttribute("aria-label", show ? "Hide token" : "Show token");
  button.innerHTML = icon(show ? "eyeOff" : "eye");
}

// ---------- options, mode, persistence ----------

const currentMode = () => $("input[name=mode]:checked")?.value || "combined";

function setMode(mode) {
  $(`input[name=mode][value="${mode === "separate" ? "separate" : "combined"}"]`).checked = true;
  updateSubmit(); // setting .checked fires no change event
}

function readOptions() {
  const options = {};
  for (const [id, [key, inverted]] of Object.entries(TOGGLES)) options[key] = $(`#${id}`).checked !== inverted;
  for (const [id, key] of Object.entries(LISTS)) options[key] = $(`#${id}`).value.trim();
  return options;
}

function applyOptions(options = {}) {
  for (const [id, [key, inverted]] of Object.entries(TOGGLES)) {
    if (typeof options[key] === "boolean") $(`#${id}`).checked = options[key] !== inverted;
  }
  for (const [id, key] of Object.entries(LISTS)) {
    if (key in options) {
      const value = options[key];
      $(`#${id}`).value = Array.isArray(value) ? value.join(", ") : value ?? "";
    }
  }
  updateOptionsSummary();
}

function updateOptionsSummary() {
  const options = readOptions();
  const changed = Object.keys(FLAG_DEFAULTS).filter((key) => options[key] !== FLAG_DEFAULTS[key]).length +
    Object.values(LISTS).filter((key) => options[key]).length;
  $("#options-summary").textContent = changed ? `${changed} customized` : "Defaults";
}

function saveForm() {
  const accounts = readAccounts().map(({ login, emails }) => ({ login, emails }));
  storageSet(FORM_KEY, JSON.stringify({ accounts, options: readOptions(), mode: currentMode() }));
}
const saveFormSoon = debounce(saveForm, 300);

function restoreForm() {
  let saved = null;
  try {
    saved = JSON.parse(storageGet(FORM_KEY) || "null");
  } catch {
    saved = null;
  }
  if (Array.isArray(saved?.accounts) && saved.accounts.length) {
    for (const account of saved.accounts.slice(0, MAX_ACCOUNTS)) {
      addAccount({ login: String(account.login || ""), emails: String(account.emails || "") });
    }
    applyOptions(saved.options);
    setMode(saved.mode);
    return;
  }
  // First visit: start from what .env already holds.
  const { env_logins: envLogins, defaults } = state.config;
  const logins = envLogins.length ? envLogins : [""];
  for (const login of logins) {
    // AUTHOR_EMAILS only maps unambiguously when .env names a single account.
    addAccount({ login, emails: logins.length === 1 ? defaults.author_emails || "" : "" });
  }
  applyOptions({
    extra_repos: defaults.extra_repos || "",
    extra_orgs: defaults.extra_orgs || "",
    exclude_owners: defaults.exclude_owners || "",
  });
}

// ---------- submit ----------

function updateSubmit() {
  const button = $("#submit");
  if (button.dataset.busy) return;
  const accounts = readAccounts();
  const separate = currentMode() === "separate" && accounts.length > 1;
  button.textContent = separate ? `Generate ${accounts.length} reports` : "Generate report";
  button.disabled = !accounts.some((account) => account.login);
}

function setBusy(busy) {
  const button = $("#submit");
  if (busy) {
    button.dataset.busy = "1";
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span>Starting…';
  } else {
    delete button.dataset.busy;
    updateSubmit();
  }
}

function validate(accounts) {
  const problems = [];
  const seen = new Set();
  for (const account of accounts) {
    const key = account.login.toLowerCase();
    if (!account.login) problems.push([account.row, "login", "Enter a GitHub username."]);
    else if (!LOGIN_RE.test(account.login)) {
      problems.push([account.row, "login", "Usernames contain only letters, numbers and hyphens."]);
    } else if (seen.has(key)) problems.push([account.row, "login", "This account is already listed."]);
    else if (account.token && !TOKEN_RE.test(account.token)) {
      problems.push([account.row, "token", "That doesn't look like a GitHub token."]);
    } else if (!account.token && !envTokenName(account.login)) {
      problems.push([account.row, "token", "Paste a token. There's none for this account in .env."]);
    } else if (splitList(account.emails).some((email) => !EMAIL_RE.test(email))) {
      problems.push([account.row, "emails", "Separate email addresses with commas."]);
    }
    seen.add(key);
  }
  return problems;
}

function clearRowError(row) {
  for (const field of $$(".field.invalid", row)) field.classList.remove("invalid");
  for (const input of $$("[aria-invalid]", row)) input.removeAttribute("aria-invalid");
  const error = $(".row-error", row);
  error.hidden = true;
  error.textContent = "";
}

function showProblems(problems) {
  for (const row of $$("#accounts .account")) clearRowError(row);
  for (const [row, name, message] of problems) {
    const input = $(`[name=${name}]`, row);
    input.closest(".field").classList.add("invalid");
    input.setAttribute("aria-invalid", "true");
    const error = $(".row-error", row);
    error.textContent = message;
    error.hidden = false;
  }
  if (problems.length) {
    const [row, name] = problems[0];
    $(`[name=${name}]`, row).focus();
  }
}

function showFormError(message) {
  const box = $("#form-error");
  box.innerHTML = `${icon("alert")}<span>${esc(message)}</span>`;
  box.hidden = false;
}

async function submit(event) {
  event.preventDefault();
  $("#form-error").hidden = true;
  const accounts = readAccounts();
  const problems = validate(accounts);
  showProblems(problems);
  if (problems.length) return;

  const payload = accounts.map(({ login, token, emails }) => ({ login, token, emails: splitList(emails) }));
  const options = readOptions();
  const batches = currentMode() === "separate" ? payload.map((account) => [account]) : [payload];
  const created = [];
  setBusy(true);
  try {
    for (const batch of batches) {
      created.push(await api("/api/jobs", { method: "POST", body: { accounts: batch, options } }));
    }
  } catch (error) {
    showFormError(error.message);
  } finally {
    setBusy(false);
  }
  if (!created.length) return;

  saveForm();
  mergeJobs(created);
  toast(created.length > 1 ? `${created.length} reports queued` : "Report started");
  $("#feed-title").scrollIntoView({ behavior: "smooth", block: "start" });
  schedulePoll(POLL_ACTIVE_MS);
}

function runAgain(job) {
  const typed = new Map(
    readAccounts().filter((a) => a.token).map((a) => [a.login.toLowerCase(), a.token]),
  );
  $("#accounts").replaceChildren();
  for (const account of job.accounts) {
    addAccount({
      login: account.login,
      emails: (account.emails || []).join(", "),
      token: typed.get(account.login.toLowerCase()) || "",
    });
  }
  applyOptions(job.options);
  setMode("combined");
  updateSubmit();
  saveFormSoon();
  window.scrollTo({ top: 0, behavior: "smooth" });
  const needsToken = $$("#accounts .account").find(
    (row) => !$("[name=token]", row).value && !envTokenName(loginOf(row)),
  );
  (needsToken ? $("[name=token]", needsToken) : $("#submit")).focus({ preventScroll: true });
}

// ---------- feed ----------

function statusChip(status) {
  const lead = status === "running" ? '<span class="spinner"></span>'
    : status === "done" ? icon("check")
    : status === "failed" ? icon("alert")
    : "";
  return `<span class="chip chip-${esc(status)}">${lead}${esc(STATUS_LABELS[status] || status)}</span>`;
}

function runningHTML(job) {
  const progress = job.progress;
  const pct = progress && progress.total ? Math.round((progress.n / progress.total) * 100) : null;
  const count = pct === null ? ""
    : `<span class="muted"> · ${progress.n.toLocaleString()} of ${progress.total.toLocaleString()}</span>`;
  const bar = pct === null
    ? `<div class="progress indeterminate" role="progressbar" aria-label="${esc(job.phase)}"><span></span></div>`
    : `<div class="progress" role="progressbar" aria-label="${esc(job.phase)}" aria-valuemin="0" ` +
      `aria-valuemax="100" aria-valuenow="${pct}"><span style="width:${pct}%"></span></div>`;
  const message = job.message ? `<p class="report-message" title="${esc(job.message)}">${esc(job.message)}</p>` : "";
  return `<p class="report-phase">${esc(job.phase)}${count}</p>${bar}${message}`;
}

function doneHTML(job) {
  const summary = job.summary || {};
  const stats = [
    [summary.total_lifetime_commits, "commit", "commits"],
    [summary.total_pull_requests, "pull request", "pull requests"],
    [summary.repositories_contributed_to, "repository", "repositories"],
    [summary.active_days, "active day", "active days"],
  ].filter(([value]) => value !== undefined && value !== "");
  const statsHTML = stats.length
    ? `<div class="stats">${stats.map(([value, one, many]) => {
      const n = Number(value);
      return `<span><b>${esc(Number.isFinite(n) ? n.toLocaleString() : value)}</b> ${n === 1 ? one : many}</span>`;
    }).join("")}</div>`
    : "";
  const first = summary.first_contribution_date;
  const range = first
    ? `<p class="report-sub">${esc(shortDate(first))} – ${esc(shortDate(summary.latest_contribution_date || first))}</p>`
    : "";
  const none = Number(summary.total_lifetime_commits) === 0
    ? `<div class="notice">${icon("alert")}<span>No commits found. If the account commits with an email that ` +
      `isn't on its GitHub profile, add it under Commit emails and run again.</span></div>`
    : "";
  return statsHTML + range + none;
}

function statusHTML(job) {
  switch (job.status) {
    case "queued":
      return '<p class="report-text muted">Waiting for the report ahead of it to finish.</p>';
    case "running":
      return runningHTML(job);
    case "done":
      return doneHTML(job);
    case "failed":
      return `<div class="alert">${icon("alert")}<span>${esc(job.error || "The report failed.")}</span></div>`;
    default:
      return '<p class="report-text muted">Cancelled before it finished.</p>';
  }
}

function warningsHTML(job) {
  if (!job.warnings?.length || job.status === "queued") return "";
  const items = job.warnings.slice(0, 3).map((warning) => `<li>${esc(warning)}</li>`).join("");
  const more = job.warnings.length > 3 ? `<li class="muted">${job.warnings.length - 3} more in the log</li>` : "";
  return `<div class="notice">${icon("alert")}<ul>${items}${more}</ul></div>`;
}

function actionsHTML(job, logOpen) {
  const files = job.files || {};
  const links = [];
  if (job.status === "done") {
    if (files.pdf) {
      links.push(`<a class="btn btn-primary btn-sm" href="${esc(files.pdf)}" download>${icon("download")}Download PDF</a>`);
    }
    if (files.html) {
      links.push(`<a class="btn ${files.pdf ? "btn-outline" : "btn-primary"} btn-sm" href="${esc(files.html)}" ` +
        `target="_blank" rel="noopener">${icon("external")}Open HTML</a>`);
    }
    if (files.xlsx) {
      links.push(`<a class="btn btn-outline btn-sm" href="${esc(files.xlsx)}" download>${icon("sheet")}Excel</a>`);
    }
  }
  const logLabel = logOpen ? "Hide log" : "Show log";
  const tools = [
    `<button type="button" class="icon-btn action" data-action="log" aria-expanded="${logOpen}" ` +
      `aria-label="${logLabel}" title="${logLabel}">${icon("log")}</button>`,
    ACTIVE.has(job.status)
      ? `<button type="button" class="icon-btn action action-danger" data-action="cancel" aria-label="Cancel" ` +
        `title="Cancel">${icon("stop")}</button>`
      : `<button type="button" class="icon-btn action" data-action="again" aria-label="Run again" ` +
        `title="Run again with these settings">${icon("redo")}</button>`,
  ];
  return `<div class="report-actions">${links.join("")}<span class="spacer"></span>${tools.join("")}</div>`;
}

function cardBody(job) {
  const names = job.logins.join(" + ");
  const combined = job.logins.length > 1
    ? `<p class="report-sub">Combined report · ${job.logins.length} accounts</p>`
    : "";
  return `<div class="report-head">` +
      `<span class="report-name" title="${esc(names)}">${esc(names)}</span>` +
      `<span class="report-meta">· <time datetime="${esc(job.created_at)}" title="${esc(fullDate(job.created_at))}">` +
      `${esc(relativeTime(job.created_at))}</time></span>${statusChip(job.status)}</div>` +
    combined + statusHTML(job) + warningsHTML(job) + actionsHTML(job, state.logs.has(job.id));
}

function createCard(job) {
  const el = document.createElement("article");
  el.className = "report";
  el.dataset.id = job.id;
  el.innerHTML = avatarHTML(job.logins[0] || "?", job.logins.length - 1) +
    '<div class="report-main"><div class="report-body"></div><div class="log" role="log" hidden></div></div>';
  const card = { el, body: $(".report-body", el), log: $(".log", el), html: "" };
  state.cards.set(job.id, card);
  return card;
}

function renderCard(card, job) {
  const html = cardBody(job);
  card.el.dataset.status = job.status;
  if (html === card.html) return;
  const focused = card.body.contains(document.activeElement) ? document.activeElement.dataset.action : null;
  card.body.innerHTML = html;
  card.html = html;
  if (focused) $(`[data-action="${focused}"]`, card.body)?.focus();
}

function renderFeed() {
  const feed = $("#feed");
  const ids = new Set(state.jobs.map((job) => job.id));
  for (const [id, card] of state.cards) {
    if (!ids.has(id)) {
      card.el.remove();
      state.cards.delete(id);
      state.logs.delete(id);
    }
  }
  state.jobs.forEach((job, index) => {
    const card = state.cards.get(job.id) ?? createCard(job);
    renderCard(card, job);
    if (feed.children[index] !== card.el) feed.insertBefore(card.el, feed.children[index] ?? null);
  });
  $("#feed-empty").hidden = state.jobs.length > 0;
}

function announce(job) {
  const who = job.logins.join(" + ");
  const text = {
    done: `Report for ${who} is ready`,
    failed: `Report for ${who} failed`,
    cancelled: `Report for ${who} was cancelled`,
  }[job.status] || `Report for ${who} finished`;
  toast(text);
  if (document.hidden) {
    state.unseen += 1;
    document.title = `(${state.unseen}) ${BASE_TITLE}`;
  }
}

function setJobs(jobs) {
  const before = new Map(state.jobs.map((job) => [job.id, job.status]));
  for (const job of jobs) {
    const previous = before.get(job.id);
    if (previous && ACTIVE.has(previous) && !ACTIVE.has(job.status)) announce(job);
  }
  state.jobs = jobs;
  renderFeed();
}

function mergeJobs(updated) {
  const byId = new Map(state.jobs.map((job) => [job.id, job]));
  for (const job of updated) byId.set(job.id, job);
  state.jobs = [...byId.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
  renderFeed();
}

// ---------- logs ----------

const LOG_LINE_RE = /^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d) \| ([A-Z]+)\s*\| \S+ \| (.*)$/;

function logLine(text) {
  const div = document.createElement("div");
  const match = LOG_LINE_RE.exec(text);
  if (match) {
    div.className = `ll ll-${match[2].toLowerCase()}`;
    div.innerHTML = `<span class="ll-time">${match[1]}</span>${esc(match[3])}`;
  } else {
    div.className = "ll";
    div.textContent = text;
  }
  return div;
}

function appendLog(box, lines) {
  if (!lines.length) {
    if (!box.childElementCount) box.innerHTML = '<div class="ll ll-empty">No output yet.</div>';
    return;
  }
  $(".ll-empty", box)?.remove();
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  const fragment = document.createDocumentFragment();
  for (const line of lines) fragment.append(logLine(line));
  box.append(fragment);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

async function pullLog(id) {
  const entry = state.logs.get(id);
  const card = state.cards.get(id);
  if (!entry || !card || entry.busy) return;
  entry.busy = true;
  try {
    const { lines, next } = await api(`/api/jobs/${encodeURIComponent(id)}/log?since=${entry.next}`);
    if (state.logs.get(id) !== entry) return; // closed meanwhile
    entry.next = next;
    appendLog(card.log, lines);
  } catch (error) {
    appendLog(card.log, [`Couldn't load the log: ${error.message}`]);
  } finally {
    entry.busy = false;
  }
}

async function toggleLog(id) {
  const card = state.cards.get(id);
  if (!card) return;
  if (state.logs.has(id)) {
    state.logs.delete(id);
    card.log.hidden = true;
  } else {
    state.logs.set(id, { next: 0, busy: false });
    card.log.replaceChildren();
    card.log.hidden = false;
    pullLog(id);
  }
  const job = state.jobs.find((j) => j.id === id);
  if (job) renderCard(card, job);
}

async function cancelJob(job) {
  try {
    mergeJobs([await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST", body: {} })]);
  } catch (error) {
    toast(error.message);
  }
  schedulePoll(300);
}

// ---------- polling ----------

function schedulePoll(ms) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(refresh, ms);
}

async function refresh() {
  try {
    const { jobs } = await api("/api/jobs");
    setJobs(jobs);
    const pulls = [];
    for (const [id, entry] of state.logs) {
      const job = jobs.find((j) => j.id === id);
      if (job && entry.next < job.log_count) pulls.push(pullLog(id));
    }
    await Promise.all(pulls);
  } catch {
    // The server may be restarting; keep trying on the normal schedule.
  }
  schedulePoll(state.jobs.some((job) => ACTIVE.has(job.status)) ? POLL_ACTIVE_MS : POLL_IDLE_MS);
}

// ---------- start ----------

async function init() {
  initTheme();
  hydrateIcons(document);
  try {
    state.config = { ...state.config, ...(await api("/api/config")) };
  } catch (error) {
    showFormError(`Couldn't reach the report server: ${error.message}`);
  }
  restoreForm();
  $("#pdf-notice").hidden = state.config.pdf_browser;
  $("#sidebar-footer").textContent =
    `Runs locally · reports are saved in ${state.config.output_dir || "output-web/"}` +
    (state.config.version ? ` · v${state.config.version}` : "");

  const composer = $("#composer");
  composer.addEventListener("submit", submit);
  composer.addEventListener("input", () => {
    updateOptionsSummary();
    saveFormSoon();
  });
  composer.addEventListener("change", () => {
    updateOptionsSummary();
    updateSubmit();
    saveFormSoon();
  });
  $("#add-account").addEventListener("click", () => $("[name=login]", addAccount()).focus());
  $("#feed").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const job = state.jobs.find((j) => j.id === button.closest(".report")?.dataset.id);
    if (!job) return;
    if (button.dataset.action === "log") toggleLog(job.id);
    else if (button.dataset.action === "cancel") cancelJob(job);
    else if (button.dataset.action === "again") runAgain(job);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      state.unseen = 0;
      document.title = BASE_TITLE;
    }
  });

  await refresh();
}

init();
