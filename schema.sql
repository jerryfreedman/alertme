-- SalesFlow CRM Schema
-- Run this in the Supabase SQL Editor

-- Accounts (companies)
create table if not exists accounts (
  id uuid default gen_random_uuid() primary key,
  name text not null,
  industry text,
  website text,
  size text,
  notes text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- Contacts (people)
create table if not exists contacts (
  id uuid default gen_random_uuid() primary key,
  first_name text,
  last_name text,
  email text,
  phone text,
  title text,
  account_id uuid references accounts(id) on delete set null,
  linkedin text,
  notes text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- Opportunities (deals)
create table if not exists opportunities (
  id uuid default gen_random_uuid() primary key,
  name text,
  account_id uuid references accounts(id) on delete set null,
  primary_contact_id uuid references contacts(id) on delete set null,
  stage text default 'prospecting', -- prospecting / qualified / proposal / negotiation / closed_won / closed_lost
  value numeric,
  currency text default 'USD',
  close_date date,
  probability integer,
  notes text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- Interactions (every touchpoint — calls, emails, meetings, notes)
create table if not exists interactions (
  id uuid default gen_random_uuid() primary key,
  type text default 'note', -- call / email / meeting / note / voicenote
  raw_text text,            -- exactly what you typed or said
  summary text,             -- claude's cleaned-up summary
  next_steps text,
  account_id uuid references accounts(id) on delete set null,
  opportunity_id uuid references opportunities(id) on delete set null,
  contact_ids uuid[],       -- array, supports multiple contacts per interaction
  created_at timestamptz default now()
);

-- Tasks / reminders
create table if not exists tasks (
  id uuid default gen_random_uuid() primary key,
  title text not null,
  due_at timestamptz,
  account_id uuid references accounts(id) on delete set null,
  opportunity_id uuid references opportunities(id) on delete set null,
  contact_id uuid references contacts(id) on delete set null,
  completed boolean default false,
  created_at timestamptz default now()
);

-- Indexes for fast lookups
create index if not exists idx_contacts_account on contacts(account_id);
create index if not exists idx_opportunities_account on opportunities(account_id);
create index if not exists idx_interactions_account on interactions(account_id);
create index if not exists idx_interactions_opportunity on interactions(opportunity_id);
create index if not exists idx_tasks_due on tasks(due_at) where completed = false;

-- Auto-update updated_at timestamps
create or replace function update_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create or replace trigger accounts_updated_at before update on accounts
  for each row execute function update_updated_at();

create or replace trigger contacts_updated_at before update on contacts
  for each row execute function update_updated_at();

create or replace trigger opportunities_updated_at before update on opportunities
  for each row execute function update_updated_at();
