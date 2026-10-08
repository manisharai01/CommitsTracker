// Contribution reports web UI. Vanilla JS, no build step; served by
// github_contrib/webapp.py. Tokens live only in the form fields: they are
// sent with a job request and never written to browser storage.

const LOGIN_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/;
const EMAIL_RE = /^[^@\s,]+@[^@\s,]+$/;
const TOKEN_RE = /^[A-Za-z0-9_]{20,255}$/;
const REPO_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/;
const PREFS_KEY = "commitstracker.prefs.v1";
const LEGACY_FORM_KEYS = ["commitstracker.form.v1", "commitstracker.form.v2"];
const THEME_KEY = "commitstracker.theme";
const ACTIVE = new Set(["queued", "running"]);
const MAX_ACCOUNTS = 20;
const POLL_ACTIVE_MS = 1200;
const POLL_IDLE_MS = 30000;
const CONFIRM_MS = 4000;
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
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>',
  share: '<path d="M12 15V3M7 8l5-5 5 5"/><path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6"/>',
  link: '<path d="M10 14a5 5 0 0 0 7.1 0l3-3a5 5 0 0 0-7.1-7.1l-1 1"/><path d="M14 10a5 5 0 0 0-7.1 0l-3 3a5 5 0 0 0 7.1 7.1l1-1"/>',
  pen: '<path d="M4 20h4L19 9l-4-4L4 16v4z"/><path d="M13.5 6.5l4 4"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  chevron: '<path d="M6 9l6 6 6-6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  alert:
    '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5M21 12H9"/>',
};
// The GitHub mark is a filled 16x16 shape, unlike the stroked icons above.
const GITHUB_MARK =
  '<path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 ' +
  "0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82" +
  "-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 " +
  "2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38" +
  ".01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 " +
  '3.58-8 8-8Z"/>';

// ?auth_error=<code> after a failed sign-in -> what the page says.
const AUTH_ERRORS = {
  denied: "Sign-in was cancelled on GitHub. Sign in again whenever you're ready.",
  state: "That sign-in attempt expired or was started in another tab. Please try again.",
  exchange: "GitHub couldn't confirm the sign-in. Please try again.",
  user: "GitHub couldn't confirm the sign-in. Please try again.",
};
const AUTH_ERROR_DEFAULT = "Sign-in didn't work. Please try again.";

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
    mode: "local",
    auth: false,
    retention_hours: 0,
    env_logins: [],
    has_default_token: false,
    pdf_browser: true,
    output_dir: "",
  },
  me: { signed_in: false }, // GitHub sign-in mode: who is signed in
  signedOut: false, // the sign-in card is showing: no polling, no form
  jobs: [],
  cards: new Map(), // job id -> { el, body, log, html }
  logs: new Map(), // job id -> { next, busy } for open log panels
  confirming: null, // job id whose delete button awaits confirmation
  confirmTimer: 0,
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
const isPublic = () => state.config.mode === "public";
const isAuth = () => state.config.auth === true;
const signedIn = () => isAuth() && state.me.signed_in === true;

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

function storageRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    // Nothing stored, or storage is blocked.
  }
}

function icon(name) {
  if (name === "github") {
    return `<svg class="icon icon-fill" viewBox="0 0 16 16" aria-hidden="true" focusable="false">${GITHUB_MARK}</svg>`;
  }
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
  if (!response.ok) {
    // The GitHub session ended (expired, or signed out in another tab).
    if (response.status === 401 && isAuth()) showSignedOut("Your session has ended. Sign in again to continue.");
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
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

// "2026-07-01" as a calendar day (no time-zone shift).
function dayFromYmd(ymd) {
  const [y, m, d] = ymd.split("-").map(Number);
  return new Date(y, m - 1, d);
}

function formatDay(day, withYear = true) {
  const options = { month: "short", day: "numeric" };
  if (withYear) options.year = "numeric";
  return day.toLocaleDateString(undefined, options);
}

function rangeText(since, until) {
  if (!since && !until) return "All time";
  if (since && until) {
    const a = dayFromYmd(since);
    const b = dayFromYmd(until);
    return `${formatDay(a, a.getFullYear() !== b.getFullYear())} – ${formatDay(b)}`;
  }
  return since ? `From ${formatDay(dayFromYmd(since))}` : `Until ${formatDay(dayFromYmd(until))}`;
}

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

// ---------- time range ----------

function ymd(day) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
}

function today() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

function addDays(day, n) {
  return new Date(day.getFullYear(), day.getMonth(), day.getDate() + n);
}

// Same day n months earlier/later, clamped to the end of shorter months.
function addMonths(day, n) {
  const first = new Date(day.getFullYear(), day.getMonth() + n, 1);
  const last = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
  return new Date(first.getFullYear(), first.getMonth(), Math.min(day.getDate(), last));
}

// Preset -> [since, until] as YYYY-MM-DD ("" = open-ended), computed when used.
const PERIODS = {
  all: () => ["", ""],
  "30d": () => [ymd(addDays(today(), -29)), ymd(today())],
  "6m": () => [ymd(addDays(addMonths(today(), -6), 1)), ymd(today())],
  "12m": () => [ymd(addDays(addMonths(today(), -12), 1)), ymd(today())],
  year: () => [`${today().getFullYear()}-01-01`, ymd(today())],
  lastyear: () => [`${today().getFullYear() - 1}-01-01`, `${today().getFullYear() - 1}-12-31`],
  custom: () => [$("#period-from").value, $("#period-to").value],
};

function browserTimezone() {
  let name = "";
  try {
    name = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    name = "";
  }
  const minutes = -new Date().getTimezoneOffset();
  const abs = Math.abs(minutes);
  const offset = `${minutes < 0 ? "-" : "+"}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
  return { name: name || offset, offset };
}

const currentPeriodKey = () => $("input[name=period]:checked")?.value || "all";

function readPeriod() {
  const [since, until] = (PERIODS[currentPeriodKey()] || PERIODS.all)();
  const zone = browserTimezone();
  return { since, until, timezone: zone.name, timezone_offset: zone.offset };
}

function updatePeriodHint() {
  const key = currentPeriodKey();
  $("#period-custom").hidden = key !== "custom";
  const { since, until } = readPeriod();
  const zone = browserTimezone().name;
  const hint = $("#period-hint");
  if (key === "custom" && !since && !until) {
    hint.textContent = "Pick a start date, an end date, or both.";
    return;
  }
  hint.textContent = `${rangeText(since, until)} · ${zone}. Commits count by the date they were written, ` +
    "pull requests by the date they were opened.";
}

function setPeriod(key, since = "", until = "") {
  const input = $(`input[name=period][value="${PERIODS[key] ? key : "all"}"]`);
  input.checked = true;
  if (key === "custom") {
    $("#period-from").value = since;
    $("#period-to").value = until;
  }
  updatePeriodHint();
}

function periodProblem() {
  if (currentPeriodKey() !== "custom") return "";
  const { since, until } = readPeriod();
  if (!since && !until) return "Pick a start date, an end date, or both.";
  if (since && until && since > until) return "The start date is after the end date.";
  return "";
}

// ---------- accounts ----------

let rowSeq = 0;
const loginOf = (row) => $("[name=login]", row).value.trim().replace(/^@/, "");

function envTokenName(login) {
  if (!login || isPublic()) return null;
  const suffix = envSuffix(login);
  if (state.config.env_logins.some((envLogin) => envSuffix(envLogin) === suffix)) return `GITHUB_TOKEN_${suffix}`;
  return state.config.has_default_token ? "GITHUB_TOKEN" : null;
}

// Whether a row may leave its token blank: only locally, when .env has a
// token for it. Signing in with GitHub never stands in for a token.
const blankTokenOk = (login) => Boolean(envTokenName(login));

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

  inputs.login.addEventListener("input", () => {
    updateInitial(row);
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
  updateInitial(row);
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

// The form shows the first letter only: loading avatars for half-typed
// usernames would request other people's pictures (and log 404s).
function updateInitial(row) {
  const login = loginOf(row);
  $(".avatar", row).dataset.initial = login ? login.charAt(0).toUpperCase() : "?";
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
      `<a href="${NEW_TOKEN_URL}" target="_blank" rel="noopener noreferrer">Create one</a></span>`;
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

// Only preferences are remembered: usernames, emails and tokens are personal
// and never stored, so every visit starts with an empty account form.
function saveForm() {
  const key = currentPeriodKey();
  const period = key === "custom" ? { key, since: $("#period-from").value, until: $("#period-to").value } : { key };
  storageSet(PREFS_KEY, JSON.stringify({ options: readOptions(), mode: currentMode(), period }));
}
const saveFormSoon = debounce(saveForm, 300);

function restoreForm() {
  // Earlier versions saved usernames and emails; erase them.
  for (const key of LEGACY_FORM_KEYS) storageRemove(key);
  let saved = null;
  try {
    saved = JSON.parse(storageGet(PREFS_KEY) || "null");
  } catch {
    saved = null;
  }
  addAccount();
  if (saved && typeof saved === "object") {
    applyOptions(saved.options);
    setMode(saved.mode);
    const period = saved.period || {};
    setPeriod(String(period.key || "all"), String(period.since || ""), String(period.until || ""));
  } else {
    setPeriod("all");
  }
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
    } else if (!account.token && !blankTokenOk(account.login)) {
      const message = isPublic()
        ? "Paste a personal access token for this account."
        : "Paste a token. There's none for this account in .env.";
      problems.push([account.row, "token", message]);
    } else if (splitList(account.emails).some((email) => !EMAIL_RE.test(email))) {
      problems.push([account.row, "emails", "Separate email addresses with commas."]);
    }
    seen.add(key);
  }
  return problems;
}

function optionsProblem(options) {
  const bad = splitList(options.extra_repos).find((repo) => !REPO_RE.test(repo));
  if (bad) return `"${bad}" is not a repository. Use owner/name.`;
  for (const key of ["extra_orgs", "exclude_owners"]) {
    const login = splitList(options[key]).find((value) => !LOGIN_RE.test(value));
    if (login) return `"${login}" is not a valid GitHub login.`;
  }
  return "";
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
  const options = readOptions();
  const problem = periodProblem() || optionsProblem(options);
  if (problem) {
    showFormError(problem);
    return;
  }

  const payload = accounts.map(({ login, token, emails }) => ({ login, token, emails: splitList(emails) }));
  const period = readPeriod();
  const batches = currentMode() === "separate" ? payload.map((account) => [account]) : [payload];
  const created = [];
  setBusy(true);
  try {
    for (const batch of batches) {
      created.push(await api("/api/jobs", { method: "POST", body: { accounts: batch, options, period } }));
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
  const since = job.options?.since || "";
  const until = job.options?.until || "";
  setPeriod(since || until ? "custom" : "all", since, until);
  setMode("combined");
  saveFormSoon();
  window.scrollTo({ top: 0, behavior: "smooth" });
  const needsToken = $$("#accounts .account").find(
    (row) => !$("[name=token]", row).value && !blankTokenOk(loginOf(row)),
  );
  (needsToken ? $("[name=token]", needsToken) : $("#submit")).focus({ preventScroll: true });
}

// ---------- feed ----------

const avatarUrl = (login) => `https://github.com/${encodeURIComponent(login)}.png?size=96`;

function avatarHTML(job) {
  const login = job.logins[0] || "?";
  const extra = job.logins.length - 1;
  const more = extra > 0 ? `<span class="avatar-more">+${extra}</span>` : "";
  // Only a finished report has proven the login exists; its picture is safe to load.
  const img = job.status === "done" ? `<img src="${esc(avatarUrl(login))}" alt="">` : "";
  return `<span class="avatar" data-initial="${esc(login.charAt(0).toUpperCase())}" aria-hidden="true">${img}${more}</span>`;
}

// A picture that fails to load is hidden so the initial shows instead.
document.addEventListener("error", (event) => {
  if (event.target instanceof HTMLImageElement && event.target.closest(".avatar")) event.target.hidden = true;
}, true);

function statusChip(status, expired = false) {
  const lead = status === "running" ? '<span class="spinner"></span>'
    : status === "done" ? icon("check")
    : status === "failed" ? icon("alert")
    : "";
  // An expired report finished fine but has nothing left to download.
  const label = expired && status === "done" ? "Done" : STATUS_LABELS[status] || status;
  return `<span class="chip chip-${esc(status)}">${lead}${esc(label)}</span>`;
}

function expiredHTML(job) {
  const text = job.status === "done"
    ? "The files for this report have expired. Run it again for a fresh copy."
    : "The details of this run have expired.";
  return `<p class="report-text expired-note">${icon("clock")}<span>${text}</span></p>`;
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
  const options = job.options || {};
  const rows = [[summary.total_lifetime_commits, "commit", "commits"]];
  if (options.pull_requests !== false) rows.push([summary.total_pull_requests, "pull request", "pull requests"]);
  rows.push([summary.repositories_contributed_to, "repository", "repositories"]);
  rows.push([summary.active_days, "active day", "active days"]);
  const stats = rows
    .filter(([value]) => value !== undefined && value !== "")
    .map(([value, one, many]) => {
      const n = Number(value);
      return `<span><b>${esc(Number.isFinite(n) ? n.toLocaleString() : value)}</b> ${n === 1 ? one : many}</span>`;
    });
  if (summary.total_lines_added) {
    const added = Number(summary.total_lines_added).toLocaleString();
    const deleted = Number(summary.total_lines_deleted || 0).toLocaleString();
    stats.push(`<span><b>+${esc(added)}</b> / <b>−${esc(deleted)}</b> lines</span>`);
  }
  const none = Number(summary.total_lifetime_commits) === 0
    ? `<div class="notice">${icon("alert")}<span>No commits found in this time range. If the account commits ` +
      `with an email that isn't on its GitHub profile, add it under Commit emails and run again.</span></div>`
    : "";
  return (stats.length ? `<div class="stats">${stats.join("")}</div>` : "") + none;
}

function statusHTML(job) {
  if (job.expired) return expiredHTML(job);
  switch (job.status) {
    case "queued":
      return '<p class="report-text muted">Waiting for the reports ahead of it to finish.</p>';
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
  // Finished reports get a row of main actions; files and tools sit below it.
  let primary = "";
  const links = [];
  if (job.status === "done" && !job.expired) {
    const main = [];
    if (files.pdf) {
      main.push(`<a class="btn btn-primary btn-sm" href="${esc(files.pdf)}" download>${icon("download")}Download PDF</a>`);
    }
    if (files.pdf || files.html) {
      main.push(`<button type="button" class="btn btn-outline btn-sm" data-action="share">${icon("share")}Share report</button>`);
    }
    main.push(`<button type="button" class="btn btn-outline btn-sm" data-action="linkedin">${icon("pen")}LinkedIn summary</button>`);
    primary = `<div class="report-actions">${main.join("")}</div>`;
    if (files.html) {
      links.push(`<a class="text-link" href="${esc(files.html)}" target="_blank" rel="noopener noreferrer" ` +
        `title="Open the HTML report in a new tab">${icon("external")}HTML</a>`);
    }
    if (files.xlsx) {
      links.push(`<a class="text-link" href="${esc(files.xlsx)}" download title="Download the Excel workbook">` +
        `${icon("sheet")}Excel</a>`);
    }
  }
  const logLabel = logOpen ? "Hide log" : "Show log";
  // An expired history entry has no log or files: only "Run again" and delete.
  const tools = job.expired ? [] : [
    `<button type="button" class="icon-btn action" data-action="log" aria-expanded="${logOpen}" ` +
      `aria-label="${logLabel}" title="${logLabel}">${icon("log")}</button>`,
  ];
  if (ACTIVE.has(job.status) && !job.expired) {
    tools.push(`<button type="button" class="icon-btn action action-danger" data-action="cancel" ` +
      `aria-label="Cancel" title="Cancel">${icon("stop")}</button>`);
  } else {
    tools.push(`<button type="button" class="icon-btn action" data-action="again" aria-label="Run again" ` +
      `title="Run again with these settings">${icon("redo")}</button>`);
    tools.push(state.confirming === job.id
      ? `<button type="button" class="btn btn-confirm" data-action="delete">Delete</button>`
      : `<button type="button" class="icon-btn action action-danger" data-action="delete" ` +
        `aria-label="Delete report" title="Delete report">${icon("trash")}</button>`);
  }
  const secondary = primary ? " report-actions-secondary" : "";
  return primary +
    `<div class="report-actions${secondary}">${links.join("")}<span class="spacer"></span>${tools.join("")}</div>`;
}

function cardBody(job) {
  const names = job.logins.join(" + ");
  const options = job.options || {};
  const range = rangeText(options.since || "", options.until || "");
  const combined = job.logins.length > 1 ? ` · ${job.logins.length} accounts combined` : "";
  const shared = job.share_url ? ' · <span class="shared-flag">Shared by link</span>' : "";
  return `<div class="report-head">` +
      `<span class="report-name" title="${esc(names)}">${esc(names)}</span>` +
      `<span class="report-meta">· <time datetime="${esc(job.created_at)}" title="${esc(fullDate(job.created_at))}">` +
      `${esc(relativeTime(job.created_at))}</time></span>${statusChip(job.status, job.expired)}</div>` +
    `<p class="report-sub report-period">${esc(range)}${esc(combined)}${shared}</p>` +
    statusHTML(job) + warningsHTML(job) + actionsHTML(job, state.logs.has(job.id));
}

function createCard(job) {
  const el = document.createElement("article");
  el.className = "report";
  el.dataset.id = job.id;
  el.innerHTML = avatarHTML(job) +
    '<div class="report-main"><div class="report-body"></div><div class="log" role="log" hidden></div></div>';
  const card = { el, body: $(".report-body", el), log: $(".log", el), html: "", status: job.status };
  state.cards.set(job.id, card);
  return card;
}

function renderCard(card, job) {
  if (card.status !== job.status) {
    // The avatar picture appears once the report is done.
    card.el.firstElementChild.outerHTML = avatarHTML(job);
    card.status = job.status;
  }
  card.el.dataset.status = job.status;
  card.el.classList.toggle("report-expired", Boolean(job.expired));
  const html = cardBody(job);
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

function toggleLog(id) {
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

function rerender(id) {
  const job = state.jobs.find((j) => j.id === id);
  const card = state.cards.get(id);
  if (job && card) renderCard(card, job);
}

async function deleteJob(job) {
  if (state.confirming !== job.id) {
    // First click arms the button; a second click within a few seconds deletes.
    const previous = state.confirming;
    state.confirming = job.id;
    if (previous) rerender(previous);
    rerender(job.id);
    clearTimeout(state.confirmTimer);
    state.confirmTimer = setTimeout(() => {
      state.confirming = null;
      rerender(job.id);
    }, CONFIRM_MS);
    return;
  }
  clearTimeout(state.confirmTimer);
  state.confirming = null;
  try {
    await api(`/api/jobs/${encodeURIComponent(job.id)}`, { method: "DELETE", body: {} });
    state.logs.delete(job.id);
    state.jobs = state.jobs.filter((j) => j.id !== job.id);
    renderFeed();
    toast("Report deleted");
  } catch (error) {
    toast(error.message);
    rerender(job.id);
  }
}

// ---------- share & LinkedIn ----------

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Older browsers or no clipboard permission: copy through a hidden field.
    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.className = "sr-only";
    document.body.append(field);
    field.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    field.remove();
    return copied;
  }
}

function pdfFileName(job) {
  return `github-report-${job.logins.slice(0, 3).join("+")}-${job.created_at.slice(0, 10)}.pdf`;
}

function canShareFiles() {
  try {
    return typeof navigator.canShare === "function" &&
      navigator.canShare({ files: [new File(["x"], "x.pdf", { type: "application/pdf" })] });
  } catch {
    return false;
  }
}

const share = { job: null, file: null };

function showShareLink(job) {
  const box = $("#share-url-box");
  box.hidden = !job.share_url;
  $("#share-url").value = job.share_url || "";
  $("#share-link-title").textContent = job.share_url ? "Copy link" : "Create link";
}

function openShare(job) {
  share.job = job;
  share.file = null;
  const files = job.files || {};
  $("#share-sub").textContent = `${job.logins.join(" + ")} · ${rangeText(job.options?.since || "", job.options?.until || "")}`;
  const download = $("#share-download");
  download.hidden = !files.pdf;
  if (files.pdf) download.href = files.pdf;

  // The device's own share sheet (mail, chat apps…) needs the file ready
  // before the click, so it is fetched as soon as the dialog opens.
  const native = $("#share-native");
  native.hidden = !(files.pdf && canShareFiles());
  if (!native.hidden) {
    native.disabled = true;
    $("#share-native-title").textContent = "Preparing PDF…";
    fetch(files.pdf)
      .then((response) => (response.ok ? response.blob() : Promise.reject(new Error("download failed"))))
      .then((blob) => {
        if (share.job !== job) return;
        share.file = new File([blob], pdfFileName(job), { type: "application/pdf" });
        native.disabled = false;
        $("#share-native-title").textContent = "Share PDF…";
      })
      .catch(() => {
        native.hidden = true;
      });
  }

  const linksOn = isPublic();
  $("#share-link").hidden = !linksOn;
  showShareLink(job);
  const hours = Number(state.config.retention_hours) || 0;
  $("#share-note").textContent = linksOn
    ? "A link stops working when you stop sharing or delete the report" +
      (hours ? `, and when the report expires (${hours} hours after it finished).` : ".")
    : "Sharing by link is available when the app is hosted for your team (public mode).";
  $("#share-dialog").showModal();
}

async function shareNative() {
  const job = share.job;
  if (!job || !share.file) return;
  try {
    await navigator.share({
      files: [share.file],
      title: "GitHub contribution report",
      text: `GitHub contribution report · ${job.logins.join(" + ")}`,
    });
  } catch (error) {
    if (error?.name !== "AbortError") toast("Couldn't open the share sheet. Download the PDF instead.");
  }
}

async function shareLink() {
  const job = share.job;
  if (!job) return;
  let url = job.share_url;
  if (!url) {
    try {
      const updated = await api(`/api/jobs/${encodeURIComponent(job.id)}/share`, { method: "POST", body: {} });
      mergeJobs([updated]);
      share.job = updated;
      url = updated.share_url;
      showShareLink(updated);
    } catch (error) {
      toast(error.message);
      return;
    }
  }
  toast((await copyText(url)) ? "Link copied" : "Copy the link from the box below");
  $("#share-url").select();
}

async function stopSharing() {
  const job = share.job;
  if (!job) return;
  try {
    const updated = await api(`/api/jobs/${encodeURIComponent(job.id)}/share`, { method: "DELETE", body: {} });
    mergeJobs([updated]);
    share.job = updated;
    showShareLink(updated);
    toast("Link turned off");
  } catch (error) {
    toast(error.message);
  }
}

function updatePostCount() {
  const text = $("#linkedin-text").value;
  const limit = Number($("#linkedin-text").dataset.limit) || 3000;
  const count = $("#linkedin-count");
  count.textContent = `${text.length.toLocaleString()} / ${limit.toLocaleString()}`;
  count.classList.toggle("over-limit", text.length > limit);
}

async function openLinkedIn(job) {
  const area = $("#linkedin-text");
  area.value = "";
  area.placeholder = "Writing your summary…";
  $("#linkedin-dialog").showModal();
  try {
    const { text, message, limit } = await api(`/api/jobs/${encodeURIComponent(job.id)}/linkedin`);
    area.dataset.limit = String(limit || 3000);
    area.value = text;
    area.placeholder = message || "";
  } catch (error) {
    area.placeholder = error.message;
  }
  updatePostCount();
}

async function copyPost() {
  const text = $("#linkedin-text").value.trim();
  if (!text) return;
  toast((await copyText(text)) ? "Copied. Paste it into a LinkedIn post." : "Select the text and copy it");
}

async function postOnLinkedIn() {
  const text = $("#linkedin-text").value.trim();
  if (!text) return;
  // Copy first (the composer may not pick up the text), then open LinkedIn.
  await copyText(text);
  const url = `https://www.linkedin.com/feed/?shareActive=true&text=${encodeURIComponent(text)}`;
  window.open(url, "_blank", "noopener,noreferrer");
  toast("Text copied. If LinkedIn's post box is empty, paste it there.");
}

function initDialogs() {
  for (const dialog of $$("dialog.sheet")) {
    // A click on the dimmed backdrop closes the dialog.
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  }
  $("#share-dialog").addEventListener("close", () => {
    share.job = null;
    share.file = null;
  });
  $("#share-native").addEventListener("click", shareNative);
  $("#share-link").addEventListener("click", shareLink);
  $("#share-stop").addEventListener("click", stopSharing);
  $("#share-url").addEventListener("focus", (event) => event.target.select());
  $("#linkedin-text").addEventListener("input", updatePostCount);
  $("#linkedin-copy").addEventListener("click", copyPost);
  $("#linkedin-open").addEventListener("click", postOnLinkedIn);
}

// ---------- polling ----------

function schedulePoll(ms) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(refresh, ms);
}

async function refresh() {
  if (state.signedOut) return;
  try {
    const { jobs } = await api("/api/jobs");
    if (state.signedOut) return;
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
  if (state.signedOut) return; // a 401 switched to the sign-in card
  schedulePoll(state.jobs.some((job) => ACTIVE.has(job.status)) ? POLL_ACTIVE_MS : POLL_IDLE_MS);
}

// ---------- GitHub sign-in ----------

// Reads ?auth_error= (set by a failed sign-in) and removes it from the address bar.
function takeAuthError() {
  const params = new URLSearchParams(location.search);
  if (!params.has("auth_error")) return "";
  const code = params.get("auth_error");
  params.delete("auth_error");
  const query = params.toString();
  history.replaceState(history.state, "", `${location.pathname}${query ? `?${query}` : ""}${location.hash}`);
  return AUTH_ERRORS[code] || AUTH_ERROR_DEFAULT;
}

function showSignedIn() {
  const me = state.me;
  state.signedOut = false;
  const avatar = $("#me-avatar");
  avatar.dataset.initial = me.login.charAt(0).toUpperCase();
  avatar.innerHTML = me.avatar_url ? `<img src="${esc(me.avatar_url)}" alt="">` : "";
  $("#me-login").textContent = me.login;
  $("#me").hidden = false;
  $("#signin").hidden = true;
  $("#workspace").hidden = false;
}

// The sign-in card replaces the form and the history. Typed tokens are dropped.
function showSignedOut(message = "") {
  state.me = { signed_in: false };
  state.signedOut = true;
  clearTimeout(state.pollTimer);
  for (const dialog of $$("dialog.sheet")) if (dialog.open) dialog.close();
  $("#accounts").replaceChildren();
  state.jobs = [];
  state.logs.clear();
  renderFeed();
  $("#workspace").hidden = true;
  $("#me").hidden = true;
  $("#signin").hidden = false;
  const box = $("#signin-error");
  box.hidden = !message;
  box.innerHTML = message ? `${icon("alert")}<span>${esc(message)}</span>` : "";
}

async function signOut() {
  const button = $("#sign-out");
  button.disabled = true;
  try {
    await api("/auth/logout", { method: "POST", body: {} });
    showSignedOut();
    toast("Signed out");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

// ---------- start ----------

function applyMode() {
  const hours = Number(state.config.retention_hours) || 0;
  const kept = hours ? ` Reports are deleted ${hours} hours after they finish.` : "";
  const version = state.config.version ? ` · v${state.config.version}` : "";
  if (isAuth()) {
    $("#intro-text").textContent =
      "Add the GitHub accounts to report on: yours or anyone's, each with its own token. " +
      "Tokens are used for this run only and never stored." + kept;
    $("#privacy-text").textContent = "Private to your GitHub account";
    const files = hours ? ` Files are deleted ${hours} hours after a report finishes; the history stays.` : "";
    $("#sidebar-footer").textContent = `Only you can see your reports, from any device.${files}${version}`;
  } else if (isPublic()) {
    $("#intro-text").textContent =
      "Add the GitHub accounts to report on. Your token is used for this run only and is never stored." + kept;
    $("#privacy-text").textContent = "Private to this browser";
    $("#sidebar-footer").textContent =
      `Only this browser can see its reports.${kept}` + (state.config.version ? ` · v${state.config.version}` : "");
  } else {
    $("#sidebar-footer").textContent =
      `Runs locally · reports are saved in ${state.config.output_dir || "output-web/"}` +
      (state.config.version ? ` · v${state.config.version}` : "");
  }
  $("#pdf-notice").hidden = state.config.pdf_browser;
}

async function init() {
  initTheme();
  hydrateIcons(document);
  initDialogs();
  try {
    state.config = { ...state.config, ...(await api("/api/config")) };
  } catch (error) {
    showFormError(`Couldn't reach the report server: ${error.message}`);
  }
  const authError = takeAuthError();
  if (isAuth()) {
    try {
      state.me = { signed_in: false, ...(await api("/api/me")) };
    } catch {
      state.me = { signed_in: false };
    }
  }
  applyMode();
  const max = ymd(today());
  $("#period-from").max = max;
  $("#period-to").max = max;
  $("#sign-out").addEventListener("click", signOut);
  if (isAuth() && !signedIn()) {
    showSignedOut(authError);
    return; // signing in reloads the page
  }
  if (signedIn()) showSignedIn();
  else $("#workspace").hidden = false;
  if (authError && signedIn()) toast(authError);
  restoreForm();

  const composer = $("#composer");
  composer.addEventListener("submit", submit);
  composer.addEventListener("input", () => {
    updateOptionsSummary();
    updatePeriodHint();
    saveFormSoon();
  });
  composer.addEventListener("change", () => {
    updateOptionsSummary();
    updatePeriodHint();
    updateSubmit();
    saveFormSoon();
  });
  $("#add-account").addEventListener("click", () => $("[name=login]", addAccount()).focus());
  $("#feed").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const job = state.jobs.find((j) => j.id === button.closest(".report")?.dataset.id);
    if (!job) return;
    const action = button.dataset.action;
    if (action === "log") toggleLog(job.id);
    else if (action === "cancel") cancelJob(job);
    else if (action === "again") runAgain(job);
    else if (action === "delete") deleteJob(job);
    else if (action === "share") openShare(job);
    else if (action === "linkedin") openLinkedIn(job);
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
