-- SalesFlow Events Table
-- Run in Supabase SQL Editor AFTER schema_patch.sql
-- Safe to run multiple times

create table if not exists events (
  id               uuid default gen_random_uuid() primary key,
  title            text not null,
  start_at         timestamptz not null,
  end_at           timestamptz,
  duration_minutes integer default 30,
  type             text default 'call',  -- call / meeting / demo / review / other
  location         text,                 -- Zoom link, phone number, address
  account_id       uuid references accounts(id) on delete set null,
  opportunity_id   uuid references opportunities(id) on delete set null,
  contact_ids      uuid[],               -- everyone in the meeting
  notes            text,
  alerted_30m      boolean default false,
  alerted_5m       boolean default false,
  created_at       timestamptz default now(),
  updated_at       timestamptz default now()
);

-- Fast lookup: today's events, upcoming events
create index if not exists idx_events_start_at
  on events(start_at);

-- Events per account (used in account context fetch)
create index if not exists idx_events_account
  on events(account_id);

-- Auto-update updated_at
drop trigger if exists events_updated_at on events;
create trigger events_updated_at
  before update on events
  for each row execute function update_updated_at();

-- Check constraint on type
alter table events
  add constraint if not exists chk_event_type
  check (type in ('call','meeting','demo','review','other'));
