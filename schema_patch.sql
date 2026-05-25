-- SalesFlow Schema Patch
-- Run this in Supabase SQL Editor (Dashboard → SQL Editor → New Query)
-- Safe to run multiple times — all statements use IF NOT EXISTS / OR REPLACE

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. DATA INTEGRITY: Check constraints on stage and interaction type
-- ─────────────────────────────────────────────────────────────────────────────

alter table opportunities
  add constraint if not exists chk_opportunity_stage
  check (stage in ('prospecting','qualified','proposal','negotiation','closed_won','closed_lost'));

alter table interactions
  add constraint if not exists chk_interaction_type
  check (type in ('call','email','meeting','note','voicenote'));


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. PERFORMANCE: Missing indexes
-- ─────────────────────────────────────────────────────────────────────────────

-- Interactions sorted by date (used on every context fetch)
create index if not exists idx_interactions_created_at
  on interactions(created_at desc);

-- Contact last name — used in dedup fuzzy search
create index if not exists idx_contacts_last_name
  on contacts(last_name);

-- Tasks per account (used in briefing and /tasks)
create index if not exists idx_tasks_account
  on tasks(account_id);

-- Opportunity stage filtering
create index if not exists idx_opportunities_stage
  on opportunities(stage);


-- ─────────────────────────────────────────────────────────────────────────────
-- 3. FULL-TEXT SEARCH: GIN index on interaction content
--    Enables fast natural-language search across all your logged interactions
-- ─────────────────────────────────────────────────────────────────────────────

alter table interactions
  add column if not exists search_vector tsvector
  generated always as (
    to_tsvector('english',
      coalesce(summary, '') || ' ' || coalesce(raw_text, '') || ' ' || coalesce(next_steps, '')
    )
  ) stored;

create index if not exists idx_interactions_fts
  on interactions using gin(search_vector);


-- ─────────────────────────────────────────────────────────────────────────────
-- 4. SMART DEFAULTS: Auto-fill probability when stage is set or changed
-- ─────────────────────────────────────────────────────────────────────────────

create or replace function auto_opportunity_probability()
returns trigger as $$
begin
  -- Only set if probability not manually provided, or if stage changed
  if new.probability is null or (old.stage is distinct from new.stage) then
    new.probability := case new.stage
      when 'prospecting'  then 10
      when 'qualified'    then 25
      when 'proposal'     then 50
      when 'negotiation'  then 75
      when 'closed_won'   then 100
      when 'closed_lost'  then 0
      else coalesce(new.probability, 10)
    end;
  end if;
  return new;
end;
$$ language plpgsql;

drop trigger if exists opp_auto_probability on opportunities;
create trigger opp_auto_probability
  before insert or update on opportunities
  for each row execute function auto_opportunity_probability();


-- ─────────────────────────────────────────────────────────────────────────────
-- 5. AUDIT: Add updated_at to interactions and tasks
-- ─────────────────────────────────────────────────────────────────────────────

alter table interactions
  add column if not exists updated_at timestamptz default now();

alter table tasks
  add column if not exists updated_at timestamptz default now();

-- The update_updated_at() function already exists from the base schema
drop trigger if exists interactions_updated_at on interactions;
create trigger interactions_updated_at
  before update on interactions
  for each row execute function update_updated_at();

drop trigger if exists tasks_updated_at on tasks;
create trigger tasks_updated_at
  before update on tasks
  for each row execute function update_updated_at();


-- ─────────────────────────────────────────────────────────────────────────────
-- 6. OPTIONAL: Verify everything looks right
-- ─────────────────────────────────────────────────────────────────────────────

-- Run this SELECT to see your indexes:
-- select indexname, tablename, indexdef from pg_indexes
-- where schemaname = 'public' order by tablename, indexname;

-- Run this to see constraints:
-- select conname, contype, conrelid::regclass as table_name
-- from pg_constraint where contype = 'c' order by table_name;
