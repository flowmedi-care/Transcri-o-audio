create table if not exists transcription_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  source text,
  created_at timestamptz default now(),
  duration_seconds numeric,
  processing_time_seconds numeric,
  model text not null,
  status text not null check (status in ('queued', 'processing', 'completed', 'failed')),
  error_message text,
  transcript text
);

create index if not exists idx_tj_user_created on transcription_jobs (user_id, created_at desc);
create index if not exists idx_tj_status on transcription_jobs (status);
