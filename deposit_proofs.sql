-- Deposit proofs: the customer sends the wasl screenshot from the tracking
-- page instead of WhatsApp → Ahmed → chat → typed into admin (index v20,
-- admin v121). Run once in Supabase → SQL Editor. admin.html detects the table
-- and shows the "وەسڵە چاوەڕوانەکان" button to every staff member once it
-- exists. Contains CREATE FUNCTION: if the editor's auto-mode classifier
-- refuses it, run it with auto mode off.
--
-- Flow: index.html (anon key) uploads the picture into the PRIVATE bucket
-- deposit-proofs under <order_code>/<timestamp>.jpg and inserts one row here
-- (order_code, claimed_iqd, image_path, note). Staff open the tab, see
-- claimed vs expected, Confirm writes orders.deposit_iqd (and nothing else:
-- the order's status is never touched) or Reject with a reason. The tracking
-- page reads the result back through track_deposit_proof(code, last4).

-- orders.id is referenced through its own type, whatever it is (the type is
-- not in the repo), so the FK is built dynamically.
do $$
declare idt text;
begin
  select format_type(a.atttypid, a.atttypmod) into idt
    from pg_attribute a
   where a.attrelid = 'public.orders'::regclass and a.attname = 'id';
  execute format($f$
    create table if not exists public.deposit_proofs (
      id            bigint generated always as identity primary key,
      order_id      %s not null references public.orders(id) on delete cascade,
      order_code    text        not null,              -- as shown on the tracking page
      claimed_iqd   integer     not null check (claimed_iqd > 0),  -- what the customer typed
      confirmed_iqd integer,                           -- what staff wrote on the order
      image_path    text        not null,              -- object name in bucket deposit-proofs
      note          text,
      status        text        not null default 'pending'
                    check (status in ('pending', 'confirmed', 'rejected')),
      reject_reason text,
      created_at    timestamptz not null default now(),
      reviewed_by   text,                              -- staff full_name
      reviewed_at   timestamptz
    )$f$, idt);
end $$;
create index if not exists deposit_proofs_status_idx on public.deposit_proofs (status, created_at desc);
create index if not exists deposit_proofs_order_idx  on public.deposit_proofs (order_id);

alter table public.deposit_proofs enable row level security;

-- Is this a code the public may attach a proof to? Existing, still open
-- order, and no pile of unreviewed proofs on it already (3 pending at most,
-- so a stranger who knows a code cannot flood the queue). Security definer:
-- the anon role has no right to read orders directly.
create or replace function public.deposit_proof_allowed(p_code text) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from orders o
                  where o.order_code = p_code
                    and o.status not in ('delivered', 'cancelled'))
     and (select count(*) from deposit_proofs p
           where p.order_code = p_code and p.status = 'pending') < 3
$$;
grant execute on function public.deposit_proof_allowed(text) to anon, authenticated;

-- The customer only sends the code; the row gets its order_id here and starts
-- as pending whatever the request said. (Runs before the RLS check below.)
create or replace function public.deposit_proofs_before_insert() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  select o.id into new.order_id from orders o where o.order_code = new.order_code;
  if new.order_id is null then
    raise exception 'unknown order code %', new.order_code using errcode = 'P0002';
  end if;
  if split_part(new.image_path, '/', 1) <> new.order_code then
    raise exception 'image must live under the order code folder' using errcode = 'P0002';
  end if;
  new.status        := 'pending';
  new.confirmed_iqd := null;
  new.reject_reason := null;
  new.reviewed_by   := null;
  new.reviewed_at   := null;
  new.created_at    := now();
  return new;
end $$;

drop trigger if exists deposit_proofs_0_before_insert on public.deposit_proofs;
create trigger deposit_proofs_0_before_insert
  before insert on public.deposit_proofs
  for each row execute function public.deposit_proofs_before_insert();

-- The public may add a proof for an existing order code; nobody anonymous
-- can read, change or delete one (no select policy for anon = empty result).
drop policy if exists "anon add deposit proof" on public.deposit_proofs;
create policy "anon add deposit proof" on public.deposit_proofs
  for insert to anon, authenticated
  with check (public.deposit_proof_allowed(order_code));

-- Any signed-in staff member (a row in public.staff) reads and reviews.
drop policy if exists "staff read deposit proofs" on public.deposit_proofs;
create policy "staff read deposit proofs" on public.deposit_proofs
  for select to authenticated
  using (exists (select 1 from public.staff s where s.id = auth.uid()));

drop policy if exists "staff review deposit proofs" on public.deposit_proofs;
create policy "staff review deposit proofs" on public.deposit_proofs
  for update to authenticated
  using      (exists (select 1 from public.staff s where s.id = auth.uid()))
  with check (exists (select 1 from public.staff s where s.id = auth.uid()));

grant insert on public.deposit_proofs to anon;
grant select, insert, update on public.deposit_proofs to authenticated;
grant usage on sequence public.deposit_proofs_id_seq to anon, authenticated;

-- The tracking page shows the state of the latest proof (code + last 4 digits
-- of the customer's phone, the same pair track_order asks for).
create or replace function public.track_deposit_proof(p_code text, p_last4 text)
returns table (status text, claimed_iqd integer, confirmed_iqd integer, reject_reason text,
               created_at timestamptz, reviewed_at timestamptz)
language sql stable security definer set search_path = public as $$
  select p.status, p.claimed_iqd, p.confirmed_iqd, p.reject_reason, p.created_at, p.reviewed_at
    from deposit_proofs p
    join orders o on o.id = p.order_id
    left join customers c on c.id = o.customer_id
   where o.order_code = p_code
     and right(regexp_replace(coalesce(c.phone, ''), '\D', '', 'g'), 4) = p_last4
   order by p.created_at desc
   limit 1
$$;
grant execute on function public.track_deposit_proof(text, text) to anon, authenticated;

-- Private bucket: the picture is a bank screenshot. Anyone may PUT one image
-- into the folder of an open order code; only staff may read them. 10 MB cap,
-- images only (the page re-encodes to JPEG ≤ 1600 px before uploading).
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
  values ('deposit-proofs', 'deposit-proofs', false, 10485760,
          array['image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif'])
  on conflict (id) do nothing;

drop policy if exists "anon upload deposit proof" on storage.objects;
create policy "anon upload deposit proof" on storage.objects
  for insert to anon, authenticated
  with check (bucket_id = 'deposit-proofs'
              and public.deposit_proof_allowed(split_part(name, '/', 1)));

drop policy if exists "staff read deposit proofs" on storage.objects;
create policy "staff read deposit proofs" on storage.objects
  for select to authenticated
  using (bucket_id = 'deposit-proofs'
         and exists (select 1 from public.staff s where s.id = auth.uid()));

-- Rejected proofs keep their picture for the record; staff may clean up.
drop policy if exists "staff delete deposit proofs" on storage.objects;
create policy "staff delete deposit proofs" on storage.objects
  for delete to authenticated
  using (bucket_id = 'deposit-proofs'
         and exists (select 1 from public.staff s where s.id = auth.uid()));
