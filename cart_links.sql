-- Cart links: the purchase queue fed from Instagram DMs (admin v109).
-- Run once in Supabase → SQL Editor. admin.html detects the table and shows
-- the "cart links" button to every staff member once it exists.

create table if not exists public.cart_links (
  id          bigint generated always as identity primary key,
  username    text        not null,               -- Instagram username, lowercase, no @
  url         text        not null,               -- the SHEIN cart share link, stored verbatim
  status      text        not null default 'queued'
              check (status in ('queued', 'bought', 'cancelled')),
  added_by    text,                               -- staff full_name who pasted it
  bought_by   text,                               -- staff full_name who ticked it
  bought_at   timestamptz,
  order_code  text,                               -- reserved: link to an order later
  created_at  timestamptz not null default now()
);

create index if not exists cart_links_status_idx on public.cart_links (status, created_at desc);
create index if not exists cart_links_user_idx   on public.cart_links (username);

alter table public.cart_links enable row level security;

-- Any signed-in staff member (a row in public.staff) can read and write.
drop policy if exists "staff manage cart links" on public.cart_links;
create policy "staff manage cart links" on public.cart_links
  for all to authenticated
  using      (exists (select 1 from public.staff s where s.id = auth.uid()))
  with check (exists (select 1 from public.staff s where s.id = auth.uid()));

-- ---------------------------------------------------------------------------
-- v110: cart preview. The share link's page carries a public collage image of
-- the cart (item count stamped on it); admin stores that URL per row.
alter table public.cart_links add column if not exists preview_url text;
