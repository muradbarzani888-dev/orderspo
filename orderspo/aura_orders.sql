-- Aura Original: Instagram DM orders (admin v119).
-- Run once in Supabase → SQL Editor. The Vercel function api/ig_webhook.py
-- writes here with the service-role key; admin.html shows the "Aura orders"
-- button to every staff member once ig_orders exists.
--
-- Flow: Meta sends every DM of the aura account to the webhook → ig_messages.
-- When staff send the saved reply that contains the trigger phrase, the
-- webhook hands the thread to Claude and stores the result in ig_orders.

-- Every DM, both directions. Media URLs from Meta expire, so images are
-- copied into the ig-media bucket and the stored copy is kept in `media`.
create table if not exists public.ig_messages (
  id          bigint generated always as identity primary key,
  mid         text        not null unique,           -- Meta message id (dedupe on retries)
  igsid       text        not null,                  -- the customer's Instagram-scoped id
  direction   text        not null check (direction in ('in', 'out')),
  text        text,
  media       jsonb       not null default '[]',     -- [{type, url, stored_url}]
  raw         jsonb,                                 -- the webhook event as received
  sent_at     timestamptz not null,
  created_at  timestamptz not null default now()
);
create index if not exists ig_messages_thread_idx on public.ig_messages (igsid, sent_at);

-- Username lookup cache (Graph API), one row per customer.
create table if not exists public.ig_customers (
  igsid       text primary key,
  username    text,
  name        text,
  updated_at  timestamptz not null default now()
);

create table if not exists public.ig_orders (
  id            bigint generated always as identity primary key,
  igsid         text        not null,
  username      text,
  customer_name text,
  phone         text,
  city          text,
  address       text,
  items         jsonb       not null default '[]',   -- [{name, variant, qty, unit_price_iqd, image_url, note}]
  total_iqd     integer,
  delivery_iqd  integer,
  notes         text,                                -- Claude's remarks for staff
  missing       jsonb       not null default '[]',   -- fields it could not find
  status        text        not null default 'new'
                check (status in ('new', 'confirmed', 'bought', 'shipped', 'delivered', 'cancelled')),
  trigger_mid   text        unique,                  -- the staff message that triggered this (dedupe)
  from_ts       timestamptz,                         -- span of the thread that was read
  to_ts         timestamptz,
  model         text,
  staff_note    text,
  updated_by    text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists ig_orders_status_idx on public.ig_orders (status, created_at desc);
create index if not exists ig_orders_user_idx   on public.ig_orders (igsid);

alter table public.ig_messages  enable row level security;
alter table public.ig_customers enable row level security;
alter table public.ig_orders    enable row level security;

-- Staff read the chat log and customers; staff read and edit orders.
drop policy if exists "staff read ig messages" on public.ig_messages;
create policy "staff read ig messages" on public.ig_messages
  for select to authenticated
  using (exists (select 1 from public.staff s where s.id = auth.uid()));

drop policy if exists "staff read ig customers" on public.ig_customers;
create policy "staff read ig customers" on public.ig_customers
  for select to authenticated
  using (exists (select 1 from public.staff s where s.id = auth.uid()));

drop policy if exists "staff manage ig orders" on public.ig_orders;
create policy "staff manage ig orders" on public.ig_orders
  for all to authenticated
  using      (exists (select 1 from public.staff s where s.id = auth.uid()))
  with check (exists (select 1 from public.staff s where s.id = auth.uid()));

-- Public bucket for the copied DM images (the webhook uploads with the
-- service-role key, which bypasses storage policies; everyone may read).
insert into storage.buckets (id, name, public)
  values ('ig-media', 'ig-media', true)
  on conflict (id) do nothing;

drop policy if exists "public read ig media" on storage.objects;
create policy "public read ig media" on storage.objects
  for select to public
  using (bucket_id = 'ig-media');
