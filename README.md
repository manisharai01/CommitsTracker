# GitHub Contribution Report

A production-quality, async Python tool that exports **every commit, pull
request, repository contribution and organization contribution** accessible to
one or more GitHub Personal Access Tokens into clean **CSV**, **Excel**,
**HTML/Markdown reports** and **charts**.

Works for **any GitHub account** — just pass `--user <login>` and set the
matching token once.

---

## Quick Start (your own account)

> **5 minutes to your first report.**

### 1. Clone and install dependencies

```bash
git clone https://github.com/YOUR_FORK/CommitsTracker.git
cd CommitsTracker
```

**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell)**
```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Create a GitHub token

Go to **GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)**.

Create a classic token with these scopes:

| Scope | Why |
| --- | --- |
| `repo` | Read your own private repos AND private repos you collaborate on |
| `read:org` | Read organization membership and org repos |

> **Fine-grained tokens will miss private/work commits.** See [Capturing private commits](#capturing-private--organization--work-commits) below.

### 3. Set your token

```bash
# macOS / Linux
export GITHUB_TOKEN=ghp_your_token_here

# Windows PowerShell
$env:GITHUB_TOKEN = "ghp_your_token_here"
```

Or copy the example and fill it in:

```bash
cp .env.example .env          # macOS/Linux
Copy-Item .env.example .env   # Windows PowerShell
```

Then edit `.env`:

```dotenv
GITHUB_TOKEN=ghp_your_token_here
```

### 4. Run

```bash
python github_report.py --user YOUR_GITHUB_LOGIN
```

Open `output/report.html` in your browser — done.

---

## Web UI

Prefer a browser to the command line? Start the local web page:

```bash
python webui.py                 # opens http://127.0.0.1:8765
python webui.py --port 9000 --no-browser
```

Add one or more GitHub usernames with their tokens (and, optionally, the commit
emails each account uses), adjust the options and press **Generate report**.
Progress streams into the page, and the finished report is ready to download as
a PDF (plus the HTML report and the Excel workbook).

- **Tokens stay local.** The page sends them only to this server, which hands
  them to the report run through environment variables and forgets them when
  the run ends. They are never written to disk or to browser storage, and the
  server only accepts requests from its own page on `127.0.0.1`.
- **`.env` still works.** The page pre-fills the logins that have a
  `GITHUB_TOKEN_<LOGIN>` (plus `AUTHOR_EMAILS`, `EXTRA_REPOS`, `EXTRA_ORGS` and
  `EXCLUDE_OWNERS`). Leave a token blank to use the one from `.env`. What you
  submit replaces those `.env` lists for that run.
- **Several accounts.** Choose **One combined report** for accounts that belong
  to the same person, or **One report per account** to queue a separate report
  for each. Runs execute one at a time.
- **History.** Each run is saved in `output-web/<run id>/` and stays listed
  after a restart. **Run again** refills the form with a past run's settings.
- PDF export uses Edge or Chrome, exactly like `--pdf`.

---

## Features

| Area | What it does |
| --- | --- |
| **Authentication** | One Personal Access Token per account, read from `GITHUB_TOKEN_<LOGIN>` or `GITHUB_TOKEN` (optionally via a `.env` file). |
| **Repository discovery** | Personal, private, organization and collaborator repos — paginated and de-duplicated. |
| **Commits** | Every commit authored by the tracked users, with repo, org, SHA, message, dates, author name/email, branch and URL. Scans all branches by default. |
| **Pull requests** | Open / closed / merged PRs with repo, title, created/merged dates, state and URL. |
| **Organizations** | Org membership plus per-org contribution counts (commits, PRs, merged PRs, repos contributed to). |
| **Statistics** | Commits per repo / year / month, by author email, by organization; PR & merged-PR counts; top repositories. |
| **Readable reports** | A self-contained **styled HTML report** (`report.html`, charts embedded — open/share in any browser) and a structured **Markdown report** (`report.md`). Every repo card has a **click-to-expand list of all its commits**. |
| **Executive summary** | A promotion-ready narrative section (key projects & impact, features delivered, profile strengths) generated deterministically from the data. |
| **Work narrative** | Per-repo "what was worked on", derived deterministically from **PR titles, humanised branch names and recurring commit keywords** — no AI/API key needed. |
| **PDF export** | `--pdf` renders `report.html` to `report.pdf` with headless Edge/Chrome — commit lists expanded, ready to submit. |
| **All repos included** | Personal repos (owned by the tracked login) are included by default; opt in to dropping them with `--exclude-own-repos` / `--exclude-owner`. `--regen` restyles reports from cached CSVs without re-collecting. |
| **Activity insights** | Active days, longest daily streak, busiest day-of-week & month, average commits per active week, and primary languages. |
| **Any user** | Works for any GitHub login via `--user <login>`. |
| **Outputs** | 5 CSV files + a 13-sheet formatted Excel workbook + HTML & Markdown reports. |
| **Charts** | Per-year, per-month, day-of-week, top-repository and per-org bar charts, plus a combined dashboard PNG. |
| **Branches** | Scans **every branch by default** (complete coverage); `--default-branch-only` for a faster run. |
| **Performance** | Async (`aiohttp`) requests with a shared concurrency limiter, GitHub rate-limit handling (primary + secondary), exponential-backoff retries and progress bars. |
| **Quality** | Python 3.12+, full type hints, structured logging, modular architecture, defensive error handling, offline test suite. |

---

## Project layout

```
CommitsTracker/
├── github_report.py            # CLI entry point
├── webui.py                    # web UI entry point (python webui.py)
├── github_contrib/             # the package
│   ├── __init__.py
│   ├── config.py               # env / token loading, AppConfig
│   ├── logging_config.py       # logging setup
│   ├── models.py               # typed dataclasses + datetime parsing
│   ├── client.py               # async GitHub API client (auth, pagination, rate limit, retry)
│   ├── discovery.py            # repository + organization discovery
│   ├── commits.py              # commit collection
│   ├── pull_requests.py        # pull request collection
│   ├── organizations.py        # org contribution aggregation
│   ├── statistics.py           # pandas statistics
│   ├── exporters.py            # CSV / Excel / text-report writers
│   ├── charts.py               # matplotlib charts + dashboard
│   ├── insights.py             # deterministic work narrative + activity insights
│   ├── htmlreport.py           # styled HTML + Markdown report generation
│   ├── report.py               # orchestration (collect → compute → export)
│   ├── pdfexport.py            # report.html → report.pdf via headless Edge/Chrome
│   ├── offline.py              # --regen: reload a previous run's CSVs
│   ├── webapp.py               # web UI server: form → queued report runs
│   └── web/                    # web UI page (HTML, CSS, JS; no build step)
├── tests/
│   ├── test_offline.py         # offline tests (no network needed)
│   └── test_webapp.py          # web UI tests (fake CLI, no network needed)
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

The generated `output/` directory contains:

```
output/
├── commits.csv
├── pull_requests.csv
├── repositories.csv
├── organizations.csv
├── contribution_summary.csv
├── github_contributions.xlsx     # 13-sheet formatted workbook
├── report.html                   # styled, self-contained, shareable report
├── report.md                     # structured Markdown report
├── summary_report.txt            # human-readable lifetime summary
├── run.log                       # full run log
└── charts/
    ├── commits_per_year.png
    ├── commits_per_month.png
    ├── commits_by_weekday.png
    ├── top_repositories.png
    ├── commits_by_organization.png
    └── dashboard.png             # combined dashboard
```

---

## Setup

### Token environment variables

The tool resolves each user's token in this order (first match wins):

| Priority | Variable name | Example |
| --- | --- | --- |
| 1 | An explicit mapping in `config.py` → `DEFAULT_USER_TOKEN_ENV` | — |
| 2 | `GITHUB_TOKEN_<LOGIN>` (login upper-cased, `-`/`.` → `_`) | `GITHUB_TOKEN_ALICE` for login `alice` |
| 3 | `GITHUB_TOKEN` | single-user fallback |

**Examples for multiple accounts:**

```dotenv
# in .env
GITHUB_TOKEN_ALICE=ghp_aaaa   # python github_report.py --user alice
GITHUB_TOKEN_BOB=ghp_bbbb     # python github_report.py --user bob
```

```bash
python github_report.py --user alice --user bob
```

---

## Usage

```bash
# Single account
python github_report.py --user YOUR_LOGIN

# Multiple accounts
python github_report.py --user alice --user bob

# All-branches (this is the DEFAULT — complete coverage)
python github_report.py --user YOUR_LOGIN

# Faster run: default branch only
python github_report.py --user YOUR_LOGIN --default-branch-only

# Force-include repos/orgs that auto-discovery might miss
python github_report.py --user YOUR_LOGIN \
    --org your-company \
    --repo colleague/private-project
```

> **Branch coverage:** every branch is scanned **by default** so nothing is
> missed. Use `--default-branch-only` for a quick run that covers just the
> default branch of each repo (5-10× faster on large accounts).

### Options

| Flag | Description | Default |
| --- | --- | --- |
| `--user LOGIN` | Restrict to one user (repeatable). Required — no built-in defaults. | — |
| `--all` | All users listed in `DEFAULT_USER_TOKEN_ENV` (empty unless you add entries). | — |
| `--output DIR` | Output directory. | `output` |
| `--concurrency N` | Max concurrent API requests. | `8` |
| `--repo OWNER/NAME` | Force-include a specific repo (repeatable). Also reads `EXTRA_REPOS`. | — |
| `--org ORG` | Force-include every accessible repo in an org (repeatable). Also reads `EXTRA_ORGS`. | — |
| `--no-search-discovery` | Disable commit-search-based repo discovery. | on |
| `--no-org-repos` | Don't enumerate every repo inside your orgs. | enumerate |
| `--all-branches` | Scan every branch (this is the default; flag kept for explicitness). | **on** |
| `--default-branch-only` | Faster: scan only each repo's default branch. | off |
| `--skip-forks` | Don't scan commits/PRs in forks. | off |
| `--no-prs` | Skip pull request collection. | off |
| `--no-commits` | Skip commit collection. | off |
| `--no-charts` | Skip chart/dashboard generation. | off |
| `--max-repos N` | Limit repositories scanned (testing). | unlimited |
| `--exclude-own-repos` | Drop repos **owned by the tracked login(s)** from the outputs, so the report shows only work in other accounts/orgs (company work). | included |
| `--exclude-owner LOGIN` | Exclude every repo owned by this login (repeatable). Also reads `EXCLUDE_OWNERS`. | — |
| `--pdf` | Also render `report.html` → `report.pdf` via headless Edge/Chrome (all commit lists expanded). | off |
| `--regen` | Rebuild all report artifacts from the CSVs of a previous run — no token/network needed. Source CSVs are left untouched. | off |
| `--log-level LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. | `INFO` |
| `--version` | Print version and exit. | — |

> **Personal vs. company repos:** by default every repository the tracked
> account can reach is included, so reports show all of your commits. If you
> only want to document work done *for the company*, exclude the account's
> own personal repos with `--exclude-own-repos`, or exclude additional owners
> with `--exclude-owner LOGIN`.

### Regenerate / restyle without re-collecting

```bash
# Tweak the report from cached CSVs (fast, offline, no token):
python github_report.py --regen --pdf                     # includes own repos (default)
python github_report.py --regen --exclude-own-repos      # drop personal repos
```

---

## Configuring default users (optional)

If you regularly run reports for the same logins, you can add them as
defaults so you don't have to type `--user` every time.

Edit `github_contrib/config.py` and add your logins to `DEFAULT_USER_TOKEN_ENV`:

```python
DEFAULT_USER_TOKEN_ENV: dict[str, str] = {
    "alice": "GITHUB_TOKEN_ALICE",
    "bob":   "GITHUB_TOKEN_BOB",
}
```

After that, `python github_report.py --all` runs for both without extra flags.

---

## How it works

1. **Authenticate & discover** — each token calls `GET /user` (validation) then
   `GET /user/repos?affiliation=owner,collaborator,organization_member&visibility=all`
   to enumerate every accessible repo, plus `GET /user/orgs` for membership.
   Repos seen by multiple tokens are merged (union of "who can reach it" is kept).
2. **Collect commits** — for every repo, `GET /repos/{owner}/{repo}/commits?author={login}`
   for each tracked login. De-duplicated by SHA per repo.
3. **Collect pull requests** — `GET /repos/{owner}/{repo}/pulls?state=all`, filtered
   to PRs opened by the tracked logins. `merged` is derived from `merged_at`.
4. **Aggregate organizations** — contribution counts are rolled up per org.
5. **Compute statistics** with pandas and **export** to CSV, Excel and charts.

### Rate limits & performance

* The authenticated REST API allows **5,000 requests/hour**. A shared
  `asyncio.Semaphore` bounds concurrency (`--concurrency`, default 8).
* When `X-RateLimit-Remaining` hits 0, or a `Retry-After` header is returned,
  the client sleeps until the reset time and resumes automatically.
* Transient errors (timeouts, 5xx) are retried with exponential backoff.

---

## Capturing private / organization / work commits

If commits you made in **private or work repositories are missing**, it is
almost always the **token type**, not the tool.  Work through this checklist:

1. **Use a classic token with `repo` + `read:org`.** Fine-grained tokens cannot
   read repos owned by other users/orgs. The tool warns you at startup if it
   detects this.

   | You want to capture… | Classic `ghp_…` (`repo`,`read:org`) | Fine-grained `github_pat_…` |
   | --- | :---: | :---: |
   | Your own public repos | ✅ | ✅ |
   | Your own private repos | ✅ | only if granted |
   | **Private repos of *other* users you collaborate on** | ✅ | ❌ |
   | **Private organization / work repos** | ✅ | only if token is scoped to that org |
   | Org membership | ✅ | ❌ unless granted |

2. **Let discovery do its work.** With a proper token the tool finds repos
   four ways:
   * `GET /user/repos` (owner + collaborator + organization_member),
   * every repo inside each org you belong to (`--no-org-repos` to skip),
   * repos surfaced by the **commit Search API** (`--no-search-discovery` to skip),
   * anything you force-include below.

3. **Force-include known repos/orgs** the discovery still misses:
   ```bash
   python github_report.py --user YOUR_LOGIN \
       --org acme-corp \
       --repo colleague/their-private-repo \
       --repo acme-corp/work-backend
   ```
   or set `EXTRA_REPOS` / `EXTRA_ORGS` in `.env`.

---

## Testing

Offline tests (no network, no tokens) validate parsing, statistics, exporters,
charts and the async pagination/rate-limit logic:

```bash
python tests/test_offline.py
python tests/test_webapp.py     # web UI: validation, job pipeline, downloads
# or, if pytest is installed:
pytest -q tests
```

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `No users specified` | Add `--user YOUR_LOGIN` to the command. |
| `Configuration error: Missing GitHub token(s)` | Set `GITHUB_TOKEN_<LOGIN>` or `GITHUB_TOKEN` (env or `.env`). |
| `Authentication failed (401)` | Token is invalid/expired or lacks scopes. Recreate it. |
| Repeated "Rate limit reached; sleeping…" | Normal for large accounts; lower `--concurrency` or wait. |
| Private/org repos missing | Token lacks `repo` / `read:org` (classic) or the equivalent fine-grained permissions. |
| `403` for specific repos | The token's account cannot access that repo; it is skipped. |

---

## License

MIT — see [LICENSE](LICENSE) if present, otherwise provided as-is.


python -m venv .venv
>> .\.venv\Scripts\Activate.ps1
>> pip install -r requirements.txt
>> python github_report.py --all
.\run.ps1