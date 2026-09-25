-- Skinstinct Voice Bot — Supabase schema
-- Run this in your Supabase project's SQL editor (Dashboard → SQL Editor → New query)

create table if not exists sessions (
    id          bigint generated always as identity primary key,
    chat_id     bigint      not null,
    input_type  text        not null check (input_type in ('text', 'voice')),
    raw_input   text        not null,
    transcribed_text text   not null,
    created_at  timestamptz not null default now()
);

create table if not exists triage_results (
    id          bigint generated always as identity primary key,
    session_id  bigint      not null references sessions(id) on delete cascade,
    score       integer     not null check (score between 0 and 10),
    feedback    text        not null,
    passed      boolean     not null,
    created_at  timestamptz not null default now()
);

create table if not exists news_hooks (
    id          bigint generated always as identity primary key,
    session_id  bigint      not null references sessions(id) on delete cascade,
    hook_text   text        not null,
    source_name text        not null,
    source_url  text        not null,
    trusted     boolean     not null,
    created_at  timestamptz not null default now()
);

create table if not exists drafts (
    id          bigint generated always as identity primary key,
    session_id  bigint      not null references sessions(id) on delete cascade,
    draft_text  text        not null,
    version     integer     not null default 1,
    created_at  timestamptz not null default now()
);

-- Indexes for common lookups
create index if not exists sessions_chat_id_idx      on sessions(chat_id, created_at desc);
create index if not exists triage_session_idx        on triage_results(session_id);
create index if not exists triage_passed_idx         on triage_results(session_id, passed);
create index if not exists hooks_session_idx         on news_hooks(session_id);
create index if not exists drafts_session_version_idx on drafts(session_id, version desc);

-- RLS: disabled for this single-user server-side bot.
-- If you enable RLS in future, add a policy that checks the anon/service role.
alter table sessions      disable row level security;
alter table triage_results disable row level security;
alter table news_hooks    disable row level security;
alter table drafts        disable row level security;
