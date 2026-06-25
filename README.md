# GitHub Contribution Report

A production-quality, async Python tool that exports **every commit, pull
request, repository contribution and organization contribution** accessible to
one or more GitHub Personal Access Tokens into clean **CSV**, **Excel** and
**chart** outputs.

It is pre-configured for two accounts — **`manisharai01`** and
**`manisharai21`** — but the user→token mapping is configurable.

---

## Features

| Area | What it does |
| --- | --- |
| **Authentication** | Separate Personal Access Tokens per account, read from `GITHUB_TOKEN_1` / `GITHUB_TOKEN_2` (optionally via a `.env` file). |
| **Repository discovery** | Personal, private, organization and collaborator repos — paginated and de-duplicated across both accounts. |
| **Commits** | Every commit authored by the tracked users, with repo, org, SHA, message, dates, author name/email, branch and URL. Optional all-branch scanning. |
| **Pull requests** | Open / closed / merged PRs with repo, title, created/merged dates, state and URL. |
| **Organizations** | Org membership plus per-org contribution counts (commits, PRs, merged PRs, repos contributed to). |
| **Statistics** | Commits per repo / year / month, by author email, by organization; PR & merged-PR counts; top repositories. |
| **Readable reports** | A self-contained **styled HTML report** (`report.html`, charts embedded — open/share in any browser) and a structured **Markdown report** (`report.md`). |
| **Work narrative** | Per-repo "what was worked on", derived deterministically from **PR titles, humanised branch names and recurring commit keywords** — so the work is explained *even when commit messages are terse*. No AI/API key needed. |
| **Activity insights** | Active days, longest daily streak, busiest day-of-week & month, average commits per active week, and primary languages. |
| **Any user** | Not hardcoded — report on **any** GitHub login via `--user <login>` with a matching token. The two accounts are just the defaults. |
| **Outputs** | 5 CSV files + a 13-sheet formatted Excel workbook + HTML & Markdown reports. |
| **Charts (bonus)** | Per-year, per-month, day-of-week, top-repository and per-org charts, plus a combined dashboard PNG. |
| **Branches** | Scans **every branch by default** (complete coverage); `--default-branch-only` for a faster run. |
| **Performance** | Async (`aiohttp`) requests with a shared concurrency limiter, GitHub rate-limit handling (primary + secondary), exponential-backoff retries and progress bars. |
| **Quality** | Python 3.12+, full type hints, structured logging, modular architecture, defensive error handling, offline test suite. |

---

## Project layout

```
CommitsTracker/
├── github_report.py            # CLI entry point
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
│   └── report.py               # orchestration (collect → compute → export)
├── tests/
│   └── test_offline.py         # offline tests (no network needed)
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
├── github_contributions.xlsx     # sheets: Commits, Pull Requests, Repositories,
│                                  #         Organizations, Summary, Yearly Stats,
│                                  #         Monthly Stats (+ extras)
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

### 1. Requirements

* **Python 3.12 or newer** (`python --version`).
* The Python packages listed in `requirements.txt`.

### 2. Create a virtual environment & install dependencies

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

### 3. Create Personal Access Tokens

For **each** account (`manisharai01`, `manisharai21`) create a token at
**GitHub → Settings → Developer settings → Personal access tokens**.

> ### ⚠️ Use a **classic** token with `repo` + `read:org`
>
> This is the single most important setup decision. Token type controls what
> the GitHub API will return:
>
> | You want to capture… | Classic `ghp_…` (`repo`,`read:org`) | Fine-grained `github_pat_…` |
> | --- | :---: | :---: |
> | Your own public repos | ✅ | ✅ |
> | Your own private repos | ✅ | only if granted |
> | **Private repos of *other* users you collaborate on** | ✅ | ❌ *(impossible)* |
> | **Private organization / work repos** | ✅ | only if token is scoped to that org |
> | Org membership (`/user/orgs`) | ✅ | ❌ unless granted |
>
> A **fine-grained** token can only reach repositories owned by **you** (or one
> organization it was explicitly created for) — so commits you made in other
> people's or companies' private repos are **invisible to the API itself**, and
> no tool can export them. **If your work commits are missing, this is why.**
> Recreate the tokens as **classic** with the `repo` and `read:org` scopes.

* **Classic token (recommended)** — scopes: `repo` and `read:org`.
  Use `public_repo` + `read:org` if you only need public data.
* **Fine-grained token** — only works for your own / a single granted org's
  repos. Grant *Contents: read*, *Metadata: read*, *Pull requests: read* on the
  repositories, plus organization *Members: read*.

> Each token only sees the data **its own account** can access. To capture
> private/org repositories for both accounts, provide both tokens. The tool
> prints a warning at startup if it detects a fine-grained token or a classic
> token missing `repo` / `read:org`.

### 4. Provide the tokens

Copy the example env file and fill in real tokens:

```bash
cp .env.example .env          # macOS/Linux
Copy-Item .env.example .env   # Windows PowerShell
```

```dotenv
GITHUB_TOKEN_1=ghp_xxxxxxxx_for_manisharai01
GITHUB_TOKEN_2=ghp_xxxxxxxx_for_manisharai21
```

…or export them directly:

```bash
export GITHUB_TOKEN_1=ghp_xxxx      # macOS/Linux
$env:GITHUB_TOKEN_1 = "ghp_xxxx"    # Windows PowerShell
```

---

## Usage

```bash
# Both users (default)
python github_report.py

# Both users (explicit)
python github_report.py --all

# A single user
python github_report.py --user manisharai01
python github_report.py --user manisharai21

# Combine users explicitly
python github_report.py --user manisharai01 --user manisharai21

# ANY GitHub user (set GITHUB_TOKEN_OCTOCAT or GITHUB_TOKEN first)
python github_report.py --user octocat

# Faster run: only each repo's default branch (skips unmerged feature branches)
python github_report.py --all --default-branch-only
```

> **Branch coverage:** every branch is scanned **by default** so nothing is
> missed. This is slower on large accounts; pass `--default-branch-only` for a
> quick run that covers just the default branch of each repo.

### Options

| Flag | Description | Default |
| --- | --- | --- |
| `--user LOGIN` | Restrict to one user (repeatable). | all users |
| `--all` | All known users. | — |
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
| `--log-level LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. | `INFO` |
| `--version` | Print version and exit. | — |

Only the tokens for the **selected** users are required. For example,
`--user manisharai01` needs only `GITHUB_TOKEN_1`.

---

## How it works

1. **Authenticate & discover** — each token calls `GET /user` (validation) then
   `GET /user/repos?affiliation=owner,collaborator,organization_member&visibility=all`
   to enumerate every accessible repo, plus `GET /user/orgs` for membership.
   Repos seen by both tokens are merged (the union of "who can reach it" is kept).
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
almost always the **token type** (see the setup warning above), not the tool.
Work through this checklist:

1. **Use a classic token with `repo` + `read:org`.** Fine-grained tokens cannot
   read repos owned by other users/orgs. The tool warns you at startup if it
   detects this.
2. **Let discovery do its work.** With a proper token the tool finds your repos
   four ways and merges them:
   * `GET /user/repos` (owner + collaborator + organization_member),
   * every repo inside each organization you belong to (`--no-org-repos` to skip),
   * repos surfaced by the **commit Search API** (`--no-search-discovery` to skip),
   * anything you force-include below.
3. **Force-include known repos/orgs** the discovery still misses:
   ```bash
   python github_report.py --all \
       --org acme-corp \
       --repo someuser/their-private-repo \
       --repo acme-corp/work-backend
   ```
   or set `EXTRA_REPOS` / `EXTRA_ORGS` in `.env`.

You can confirm what a token can actually see at any time — the startup log
prints the token kind, discovered repo counts and org membership.

## Notes & limitations

* The commit `author` filter matches the **GitHub login**. Commits whose author
  email is not linked to the GitHub account won't be attributed by the API's
  filter — this is a GitHub API characteristic, not a tool bug.
* By default only the **default branch** is scanned (the common case and far
  kinder to rate limits). Use `--all-branches` for exhaustive branch coverage.
* Line-level additions/deletions are intentionally not fetched (they require one
  extra request per commit) to keep large accounts within rate limits.

---

## Testing

Offline tests (no network, no tokens) validate parsing, statistics, exporters,
charts and the async pagination/rate-limit logic:

```bash
python tests/test_offline.py
# or, if pytest is installed:
pytest -q
```

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Configuration error: Missing GitHub token(s)` | Set `GITHUB_TOKEN_1` / `GITHUB_TOKEN_2` (env or `.env`). |
| `Authentication failed (401)` | Token is invalid/expired or lacks scopes. Recreate it. |
| Repeated "Rate limit reached; sleeping…" | Normal for large accounts; lower `--concurrency` or wait. |
| Private/org repos missing | Token lacks `repo` / `read:org` (classic) or the equivalent fine-grained permissions. |
| `403` for specific repos | The token's account cannot access that repo; it is skipped. |

---

## License

Provided as-is for the requested reporting task.
