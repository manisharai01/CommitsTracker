# GitHub Contribution Report
CommitsTracker is a Python tool that produces accurate, shareable reports of a developer's GitHub contributions (commits, pull requests and repositories) for any time range. It was built for promotion reviews at Sheeraj CodeWorks. Company code there is spread across colleagues' accounts and organizations, so no single GitHub page shows a developer's full work.
How it works. Using each account's GitHub personal access token, it:
Discovers every repository the account can reach. It also finds upstream projects through GitHub search, by username, commit email and pull request. Searches are split by date to get past GitHub's 1,000-result limit.
Collects commits from every branch and reads each pull request's commits, which recovers work on deleted branches. It also fetches lines added and deleted.
Filters to the chosen time range and drops excluded owners.
Counts each change once. The same commit in a fork and its upstream counts once, as do rebased or cherry-picked copies. The original commits of a squash-merged pull request are folded into the squash commit.
Computes statistics in the user's time zone.
What it produces. It writes a PDF report, an HTML report with charts built in, a Markdown report, an Excel workbook and CSV files. The report includes:
headline numbers and a dashboard for each repository;
an executive summary written from the data;
activity insights and a breakdown of work in each repository;
a complete commit timeline.
Two sections explain the numbers. Data completeness lists anything that could not be read, and How this report was compiled states the period, branches, filters and de-duplication used.
Accuracy. Nothing is dropped silently: every repository, branch, search or request that fails becomes a visible note. On real data, every commit that GitHub's own search knew about appeared in the report. Each option (default branch only, time range, owner exclusions) matched a count made separately.
Two ways to use it.
Command line: python github_report.py --user LOGIN --since 2026-01-01 --until 2026-06-30 --timezone Asia/Kolkata --pdf. The --regen option rebuilds reports offline from saved data.
Web page: python webui.py opens a clean page styled like X/Twitter. Enter one or more usernames, tokens and optional commit emails, choose a time range and options, and download the PDF. Progress shows live, and past reports can be run again or deleted.
Hosting for other people. Public mode (--public-url https://…) serves the page to many users behind an HTTPS proxy:
Each browser session sees only its own reports.
Every user must enter a token. Tokens are never stored or logged, and the server's own .env file is never used.
Requests from other websites are rejected, and users and addresses have rate limits. Runs time out, and reports are deleted after 24 hours.
Reports open in a sandbox, and strict security headers are set. Spreadsheet downloads can't run hidden formulas.
Technology and quality. It uses Python 3.12+ with asyncio and aiohttp (for the GitHub API and the web server), plus pandas, matplotlib and openpyxl. The front end is plain JavaScript with no build step. Automated tests cover collection edge cases (the GraphQL collector is checked against the REST one), reports, sign-in and web security. The tool was also checked against live GitHub data and in a real browser, with no console errors.
Known limits. It cannot see:
commits where the user is only a co-author (GitHub has no API for this);
private repositories the token cannot read;
commits made with an email that was not entered.

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
emails each account uses), pick a **time range** (all time, last 30 days, last
6 or 12 months, this year, last year or custom dates — in your browser's time
zone), adjust the options and press **Generate report**. Progress streams into
the page, and the finished report is ready to download as a PDF (plus the HTML
report and the Excel workbook).

- **Tokens stay local.** The page sends them only to this server, which hands
  them to the report run through environment variables and forgets them when
  the run ends. They are never written to disk or to browser storage, and the
  server only accepts requests from its own page on `127.0.0.1`.
- **The form always starts empty.** Nothing is filled in from `.env`, and the
  page remembers only your options and time range — never usernames, emails or
  tokens. In local mode you can still leave a token blank to use
  `GITHUB_TOKEN_<LOGIN>` from `.env` (the page says so once you type that
  username). Web runs use only what you type: `.env`'s `AUTHOR_EMAILS`,
  `EXTRA_REPOS`, `EXTRA_ORGS` and `EXCLUDE_OWNERS` apply to the command line only.
- **Several accounts.** Choose **One combined report** for accounts that belong
  to the same person, or **One report per account** to queue a separate report
  for each.
- **History.** Each run is saved in `output-web/<run id>/` and stays listed
  after a restart. **Run again** refills the form with a past run's settings;
  the trash icon deletes a report and its files.
- **Share report.** Send the PDF through your device's share sheet (mail,
  Teams, WhatsApp…) or download it. When the app is hosted (public mode) you
  can also create a **read-only link**: whoever opens it sees a summary page
  with the PDF and the HTML report — never the logs, the Excel/CSV data or the
  commit emails — and gets no session. **Stop sharing** turns the link off at
  once; deleting or expiring the report does too.
- **LinkedIn summary.** A first-person post drafted from the report's numbers,
  for example *"During H1 2026, I contributed to 18 repositories across 6
  engineering projects, shipping 412 commits and 57 pull requests (51
  merged)."* Edit it in place, then **Copy text** or **Open LinkedIn**.
  Repository names and pull-request titles are never included, so a post
  cannot reveal private company work. The command line writes the same draft
  to `linkedin_post.txt`.
- PDF export uses Edge or Chrome, exactly like `--pdf`.

### Hosting it for other people (production)

**On Render with "Sign in with GitHub" and a report history in Postgres
(Supabase): follow [DEPLOY.md](DEPLOY.md).** Signing in only identifies the
user (it asks GitHub for no permissions), so their report history (only ids,
dates, period and status) follows their account. Reports work exactly as in
public mode: any accounts, the user's own included or not, each with its own
token.

Otherwise, run the server in **public mode** behind an HTTPS reverse proxy (nginx, Caddy,
a cloud load balancer). Public mode is designed for untrusted, concurrent users:

```bash
python webui.py --public-url https://reports.example.com --trust-proxy --no-browser
# listens on 127.0.0.1:8765; point the proxy at it
```

| What | Public mode behaviour |
| --- | --- |
| Isolation | Each browser gets a random session (`__Host-` cookie: HttpOnly, Secure, SameSite=Strict). It sees, downloads, cancels and deletes **only its own** reports — other ids answer 404. |
| Tokens | Required for every account; `.env` is **never** read (not by the server, not by runs). A run inherits only operating-system variables, never the server's secrets. Tokens are never written to disk, logs, URLs or browser storage, and are scrubbed from `run.log`. |
| Cross-site requests | `POST`/`DELETE` must come from `--public-url` (Origin check) with a JSON body. |
| Limits | 2 reports queued/running per session, 20 per client address per hour, 50 in total, 2 running at once, 2 h per run (process tree killed). All configurable. |
| Retention | Reports are deleted 24 h after they finish (`--retention-hours`). |
| Share links | Created only by a report's owner, 192-bit random, read-only (summary page, PDF, HTML), not indexed by search engines (`X-Robots-Tag: noindex`), no cookies for viewers; they stop working when sharing is turned off or the report is deleted or expires. |
| Headers | Strict CSP (no inline script), HSTS, `X-Frame-Options: DENY`, COOP/CORP, `nosniff`, `no-referrer`, `no-store` on the API; the server version is hidden. |
| Reports | `report.html` opens in a CSP **sandbox** (no cookies, storage or network), and the page's own CSP only allows its one script. Excel/CSV cells can never run as formulas. Logs shown to users hide server paths. |

Requirements: `--public-url` must be `https://` (plain HTTP is accepted only for
`localhost` testing); the proxy must forward `X-Forwarded-For` when you pass
`--trust-proxy`. To terminate TLS in the app instead, add `--tls-cert` and
`--tls-key`. Run it as an unprivileged user (headless Chrome needs no root).
See `python webui.py --help` for every limit.

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
| **Performance** | GraphQL collection (batched queries, branch comparison, 25 commits' line stats per query) with REST as the fallback; async (`aiohttp`) requests with a shared concurrency limiter, rate-limit handling (primary + secondary), exponential-backoff retries and progress bars. |
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
│   ├── graphql.py              # GraphQL collection (commits, PRs, line stats) with REST fallback
│   ├── commits.py              # REST commit collection
│   ├── pull_requests.py        # REST pull request collection
│   ├── organizations.py        # org contribution aggregation
│   ├── statistics.py           # pandas statistics
│   ├── exporters.py            # CSV / Excel / text-report writers
│   ├── charts.py               # matplotlib charts + dashboard
│   ├── insights.py             # deterministic work narrative + activity insights
│   ├── htmlreport.py           # styled HTML + Markdown report generation
│   ├── filters.py              # exclusions, time range, count-once rules, time zone
│   ├── linkedin.py             # LinkedIn post drafted from the report's numbers
│   ├── report.py               # orchestration (collect → compute → export)
│   ├── pdfexport.py            # report.html → report.pdf via headless Edge/Chrome
│   ├── offline.py              # --regen: reload a previous run's CSVs
│   ├── webapp.py               # web UI server: form → queued report runs
│   ├── auth.py                 # "Sign in with GitHub" (OAuth App) + encrypted session cookie
│   ├── store.py                # report history (Postgres / in memory)
│   └── web/                    # web UI page (HTML, CSS, JS; no build step)
├── tests/
│   ├── test_offline.py         # offline tests (no network needed)
│   ├── test_accuracy.py        # collection & counting edge cases (fake GitHub API)
│   ├── test_graphql.py         # GraphQL collector checked against the REST collector
│   ├── test_linkedin.py        # LinkedIn summary wording and periods
│   ├── test_auth.py            # sign-in, sessions, report history
│   └── test_webapp.py          # web UI tests (fake CLI, no network needed)
├── Dockerfile, render.yaml     # Render deployment (see DEPLOY.md)
├── schema.sql                  # the two history tables
├── requirements.txt
├── .env.example
├── .gitignore
├── DEPLOY.md
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

# A time range (inclusive), in your time zone
python github_report.py --user YOUR_LOGIN \
    --since 2026-01-01 --until 2026-06-30 --timezone Asia/Kolkata
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
| `--no-commit-stats` | Skip line stats (additions/deletions/files changed). | off |
| `--rest-api` | Collect with the REST API only (the previous, much slower engine). | GraphQL |
| `--max-repos N` | Limit repositories scanned (testing). | unlimited |
| `--exclude-own-repos` | Drop repos **owned by the tracked login(s)** from the outputs, so the report shows only work in other accounts/orgs (company work). | included |
| `--exclude-owner LOGIN` | Exclude every repo owned by this login (repeatable). Also reads `EXCLUDE_OWNERS`. | — |
| `--since DATE` | Only report work from this date on (`YYYY-MM-DD`, inclusive, or an ISO 8601 timestamp). Commits count by author date, pull requests by the date they were opened. | all time |
| `--until DATE` | Only report work up to this date (inclusive: the whole day counts). | all time |
| `--timezone TZ` | Time zone for `--since`/`--until` and for days, weeks and months in the report — an IANA name (`Asia/Kolkata`) or an offset (`+05:30`). | `UTC` |
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
2. **Search** — the Search API adds upstream projects: repositories with commits
   by the tracked logins **or the extra commit emails**, and repositories where
   they opened pull requests. Search returns at most 1000 results per query, so
   the date range is split until every slice is read completely.
3. **Collect pull requests** (GraphQL) — each tracked user's own pull-request
   list, with every pull request's commits. It covers private repositories the
   token can read and upstream projects alike. When every branch is scanned,
   those commits **recover work on deleted branches**.
4. **Collect commits** (GraphQL) — the default branch's history is read once
   per tracked login (and once for all extra emails); every other branch (each
   distinct head once) is **compared with the default branch**, so only the
   commits that exist on that branch alone are read. A branch more than 1,000
   commits ahead, or with no history in common (`gh-pages`), is read per author
   instead. Upstream projects only have their default branch scanned.
5. **Line statistics** (GraphQL) — 25 commits per query, for the commits inside
   the period (merge commits are skipped: their diff repeats the merged branch).

Many repositories, branches or commits share one GraphQL query. A query GitHub
can't finish in time is split, then its page shrunk, and retried; anything
still unreadable is collected again with the REST endpoints
(`GET /repos/{owner}/{repo}/commits?author=…&sha=…`, `/pulls`, `/commits/{sha}`),
which also record the data-completeness notes. `--rest-api` uses only those
REST endpoints (the previous engine).
6. **Report** — owner exclusions, then the time range, then **each change is
   counted once** (below), then dates are expressed in the report time zone;
   statistics, Excel, charts, HTML/Markdown and PDF all use the same data.

### Accuracy guarantees

* **No silent gaps.** Anything that could not be read — a branch, a repository,
  a search slice, a pull request, line stats, a token without `repo`/`read:org`
  scope or SSO authorization — becomes a note in the report's **Data
  completeness** section (and the CLI summary). A clean report says "No gaps
  detected".
* **Each change counted once.** The same commit in a fork and its upstream
  counts once (the upstream copy is kept); rebased or cherry-picked copies
  (same author, author time and subject in one repository) count once; the
  original commits of a squash-merged pull request are folded into the squash
  commit. Every report lists how many copies were folded.
* **Time range.** Commits count by author date and pull requests by creation
  date, both bounds inclusive, in the chosen time zone — the same time zone is
  used for active days, streaks, weekdays and months.
* **Transparent method.** "How this report was compiled" states the period,
  branch coverage, email matching, forks, pull requests, line statistics,
  exclusions and de-duplication used for the numbers.
* **Not covered:** commits authored by someone else with you as `Co-authored-by`
  (GitHub offers no API to list them), private repositories the tokens cannot
  read, and commits made with an email you did not list.

### Rate limits & performance

* GitHub gives each token **5,000 REST requests and 5,000 GraphQL points per
  hour**. The REST engine needed one request per page of (repository × branch ×
  author) plus one per commit for line stats: about 7,000 for an account with
  2,000 commits, so it slept for most of an hour (or several) waiting for the
  limit to reset. The GraphQL engine reads the same data in a few hundred
  points, so reports finish in minutes and a token can run many per hour.
* Measured on the same accounts, with identical CSVs from both engines:

  | Run | REST engine | GraphQL engine |
  | --- | --- | --- |
  | 17 repos, 74 commits, 14 PRs | 342 requests, 28 s | 36 requests, 15 s |
  | `cli/cli`, 266 branches, 1,385 commits | 1,559 requests, 163 s | 80 requests, 23 s |
  | `nodejs/node`, 52 release branches | 544 requests, 93 s | 130 requests, 72 s |

* Work is spread across every token that can see a repository. A shared
  `asyncio.Semaphore` bounds concurrency (`--concurrency`, default 8).
* When a limit is reached (REST headers, or GraphQL's `RATE_LIMITED` error) the
  client sleeps until the reset time and resumes automatically. Transient
  errors are retried with exponential backoff.
* The commit Search API (repository discovery) allows 30 requests a minute and
  pages through every matching commit. That costs seconds for most accounts,
  but minutes for one with tens of thousands of commits; `--since` narrows it.

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
python tests/test_accuracy.py   # collection & counting edge cases (fake GitHub API)
python tests/test_linkedin.py   # LinkedIn summary wording and periods
python tests/test_webapp.py     # web UI: validation, job pipeline, sessions, security
python tests/test_graphql.py    # GraphQL collector vs the REST collector, timeouts, fallbacks
python tests/test_auth.py       # GitHub sign-in, sessions, report history
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
| Repeated "Rate limited; sleeping…" | The token's hourly budget is used up (other tools or runs share it). The run resumes by itself; make sure `--rest-api` isn't set. |
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
