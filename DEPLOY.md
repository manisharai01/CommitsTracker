# Deploying the web app (Render + Supabase + Sign in with GitHub)

This puts `webui.py` online in public mode: people sign in with GitHub, run
reports, and see their report history from any device. You need three free
accounts: GitHub, [Supabase](https://supabase.com) and [Render](https://render.com).

Pick the service name first (for example `commitstracker`). Its address will
be `https://<service>.onrender.com`. It's used in step 1.

## 1. Create a GitHub OAuth App

1. GitHub → **Settings → Developer settings → OAuth Apps → New OAuth App**
   (for an organization: the org's **Settings → Developer settings**).
2. Fill in:
   - **Application name**: CommitsTracker (people see it on the sign-in screen)
   - **Homepage URL**: `https://<service>.onrender.com`
   - **Authorization callback URL**: `https://<service>.onrender.com/auth/callback`
3. **Register application**, then **Generate a new client secret**.
4. Keep the **Client ID** and the **client secret** for step 3. The secret is
   shown only once.

## 2. Create the database (Supabase)

1. Supabase → **New project**. Choose a region near your Render region, and
   save the database password.
2. Open **Connect** (top of the project page) → **Session pooler** and copy
   the connection string. It looks like this:
   ```
   postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
   ```
   Use the **Session pooler**, not the direct connection: the direct host
   `db.<ref>.supabase.co` is IPv6-only, and Render can't reach IPv6 addresses.
3. Put your password in place of `[YOUR-PASSWORD]`. If the password contains
   special characters, URL-encode them (`@` → `%40`, `:` → `%3A`, `/` → `%2F`,
   `#` → `%23`, `?` → `%3F`, `%` → `%25`). An easier option is to reset the
   password to letters and digits only.
4. Optional: open the **SQL editor**, paste [`schema.sql`](schema.sql) and run
   it. The app also creates the tables when it starts.

## 3. Deploy on Render

1. Push this repository to GitHub.
2. Render → **New → Blueprint** → pick the repository. Render reads
   [`render.yaml`](render.yaml) and creates the web service. (Or **New → Web
   Service** → the repository → **Docker**, then add the variables below by hand,
   with health check path `/healthz`.)
3. When asked, enter:
   - `DATABASE_URL`: the Session pooler string from step 2
   - `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`: from step 1

   `SESSION_SECRET` is generated for you.
4. Deploy. The first build takes a few minutes (it installs Chromium for PDFs).
5. Open `https://<service>.onrender.com/healthz`. It should show `ok`.
   If your service got a different address, update both URLs in the OAuth App.

## 4. First sign-in and smoke test

1. Open `https://<service>.onrender.com` and click **Sign in with GitHub**.
   Approve the app. For organization repositories, click **Grant** next to
   each organization on that screen (or ask an org owner to approve the app).
2. You're back on the page with your avatar at the top and your login in the
   first account row. Leave the token blank, pick **Last 30 days**, and click
   **Generate report**.
3. When it's ready, download the PDF. In Render → **Logs** you should see no
   errors, and a `report history: connected to the database` line.
4. Sign out and sign in again from another browser: the report is listed.

## Environment variables

| Variable | Required | What it does |
| --- | --- | --- |
| `GITHUB_CLIENT_ID` | yes | OAuth App client ID. With the secret, turns on "Sign in with GitHub". |
| `GITHUB_CLIENT_SECRET` | yes | OAuth App client secret. |
| `SESSION_SECRET` | yes | Random string, 16+ characters, that encrypts the sign-in cookie. Changing it signs everyone out. The app won't start without it when sign-in is on. |
| `DATABASE_URL` | recommended | Postgres URL for the report history. Without it, history is kept in memory and lost on restart. |
| `PUBLIC_URL` | no | The site address, if you use a custom domain. Default: `RENDER_EXTERNAL_URL`. |
| `GITHUB_OAUTH_SCOPES` | no | Scopes asked at sign-in. Default `repo read:org`. |
| `RETENTION_HOURS` | no | Hours to keep report files after a run finishes. Default 24. |
| `PARALLEL` | no | Reports run at the same time. `render.yaml` sets 1, which fits 512 MB. |
| `PORT`, `RENDER`, `RENDER_EXTERNAL_URL` | set by Render | Port, `0.0.0.0` binding and trusted proxy headers. |

Other limits are `webui.py` flags (`python webui.py --help`), for example
`--max-jobs-per-hour` and `--job-timeout`.

## Plans

- **Free** (`render.yaml` default) needs no card. It sleeps after 15 idle
  minutes, and the next visit waits about a minute while it wakes up. A
  report in progress keeps it awake (the page checks on it every second).
- **Starter** (paid, set `plan: starter`) stays awake and has more CPU, so
  reports and PDFs finish faster. Render asks for a card for it.
- On every plan, report **files** are kept on the service's own disk, which is
  wiped by each deploy, restart or sleep. The **history** (date, period,
  status) is in the database, so old entries stay listed as "expired" with a
  **Run again** button.
- Supabase's free tier pauses a project after a week without activity. Resume
  it from the Supabase dashboard if the history stops loading.

## Security notes

- **Why `repo`**: GitHub has no read-only scope for private repositories.
  `repo` and `read:org` let the report count private and organization work.
  The app only reads.
- **The token** stays in an encrypted, HttpOnly cookie (Fernet, key derived
  from `SESSION_SECRET`) that expires after 14 days. During a run, it's in
  server memory and in the report process's environment only. It is never
  written to disk, the database, logs or command lines.
- **Sign out** clears the cookie and revokes the token at GitHub. If a report
  is still running, the token is revoked when it finishes. Users can also
  revoke access at any time at github.com → Settings → Applications.
- **The database** holds only GitHub user ids and, per report: id, time,
  period and status. Row level security is on with no policies, so Supabase's
  public REST API can't read it.
