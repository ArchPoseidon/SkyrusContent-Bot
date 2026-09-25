-- Grant the anon role write access to all bot tables.
-- Run this in your Supabase SQL editor.
-- Required because the publishable/anon key is used server-side.

grant select, insert, update, delete
    on sessions, triage_results, news_hooks, drafts
    to anon;

grant usage on sequence
    sessions_id_seq,
    triage_results_id_seq,
    news_hooks_id_seq,
    drafts_id_seq
    to anon;
