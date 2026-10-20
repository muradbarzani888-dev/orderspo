-- Cart links from the website: orderspo.com/order lets a customer queue their
-- own SHEIN links while Instagram DMs are blocked (index v21, order.html,
-- admin v123). Run once in Supabase → SQL Editor. Contains CREATE FUNCTION:
-- if the editor's auto-mode classifier refuses it, run it with auto mode off.
--
-- Flow: order.html (anon key) inserts one cart_links row per link with
-- source = 'web' and the customer's name / phone / city / note. Staff see
-- those rows in the same "cart links" queue as the DM ones, marked web, and
-- confirm the order with the customer on WhatsApp. Nothing anonymous can
-- read, change or delete a row (no select policy for anon = empty result).

alter table public.cart_links add column if not exists source        text not null default 'ig'
  check (source in ('ig', 'web'));
alter table public.cart_links add column if not exists customer_name text;
alter table public.cart_links add column if not exists phone         text;   -- 07xxxxxxxxx, digits only
alter table public.cart_links add column if not exists city          text;
alter table public.cart_links add column if not exists note          text;
-- the v110 preview column (second block of cart_links.sql) was never run on
-- the live project; the trigger below clears it, so it must exist.
alter table public.cart_links add column if not exists preview_url   text;

create index if not exists cart_links_phone_idx on public.cart_links (phone, created_at desc)
  where phone is not null;

-- Web rows are validated and normalised here, before the RLS check below
-- (which only sees the row this trigger returns). Security definer: the anon
-- role may not read the table, but the hourly cap needs a count.
create or replace function public.cart_links_before_insert() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if new.source is distinct from 'web' then
    return new;                                   -- staff rows (admin) are untouched
  end if;

  -- phone: Arabic-Indic digits, spaces and a +964 / 00964 prefix are tolerated,
  -- the stored value is the plain 11-digit Iraqi mobile number.
  new.phone := translate(coalesce(new.phone, ''), '٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789');
  new.phone := regexp_replace(new.phone, '\D', '', 'g');
  new.phone := regexp_replace(new.phone, '^(00)?964', '0');
  if new.phone !~ '^07\d{9}$' then
    raise exception 'phone must be an Iraqi mobile number (07xxxxxxxxx)' using errcode = 'P0001';
  end if;

  new.url           := left(btrim(new.url), 2000);
  if new.url !~* '^https?://' then
    raise exception 'url must start with http(s)://' using errcode = 'P0001';
  end if;
  new.customer_name := nullif(left(btrim(coalesce(new.customer_name, '')), 80), '');
  if new.customer_name is null then
    raise exception 'name is required' using errcode = 'P0001';
  end if;
  new.city          := nullif(left(btrim(coalesce(new.city, '')), 60), '');
  new.note          := nullif(left(btrim(coalesce(new.note, '')), 500), '');
  new.username      := coalesce(nullif(lower(btrim(new.username)), ''), new.phone);

  -- Nothing the public sends may pre-set the queue state.
  new.status      := 'queued';
  new.added_by    := 'web';
  new.bought_by   := null;
  new.bought_at   := null;
  new.order_code  := null;
  new.preview_url := null;
  new.created_at  := now();

  -- 20 links per phone per hour: a whole cart is a handful of links, more is
  -- a script. The error text is what order.html looks for.
  if (select count(*) from cart_links c
       where c.phone = new.phone and c.created_at > now() - interval '1 hour') >= 20 then
    raise exception 'rate limit: too many links for this phone in the last hour' using errcode = 'P0001';
  end if;
  return new;
end $$;

drop trigger if exists cart_links_0_before_insert on public.cart_links;
create trigger cart_links_0_before_insert
  before insert on public.cart_links
  for each row execute function public.cart_links_before_insert();

-- The public may add web rows only; the staff policy in cart_links.sql keeps
-- covering every other operation for signed-in staff.
drop policy if exists "anon add web cart link" on public.cart_links;
create policy "anon add web cart link" on public.cart_links
  for insert to anon
  with check (source = 'web');

grant insert on public.cart_links to anon;
grant usage on sequence public.cart_links_id_seq to anon;
