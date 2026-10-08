-- CommitsTracker report history (hosted web app with "Sign in with GitHub").
--
-- Optional: the app runs these same statements at startup (they are
-- idempotent). Paste this into the Supabase SQL editor to create the tables
-- by hand. Keep it in sync with SCHEMA_SQL in github_contrib/store.py.
--
-- Stored on purpose: nothing but ids, times, the period and the status.
-- No tokens, logins, emails, report files, summaries or options.

create table if not exists users (
    id         bigint primary key,                 -- the GitHub numeric user id
    created_at timestamptz not null default now()
);

create table if not exists reports (
    id         uuid primary key,                   -- = the job id (output-web/<id>/)
    user_id    bigint not null references users(id) on delete cascade,
    created_at timestamptz not null default now(),
    period     text not null,                      -- "<since>..<until>"; ".." = all time
    status     text not null                       -- queued | running | done | failed | cancelled
);

create index if not exists reports_user_created_idx on reports (user_id, created_at desc);

-- Supabase exposes the public schema through its REST API. Row level security
-- with no policies denies every request made that way; the app connects as the
-- postgres role, which bypasses RLS.
alter table users enable row level security;
alter table reports enable row level security;
