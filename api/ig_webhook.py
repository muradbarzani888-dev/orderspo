"""Aura Original: Instagram DMs → orders (admin v119).

Meta posts every DM of the aura.originall account here (Instagram API with
Instagram Login, webhook field `messages`). Every event is stored in
Supabase `ig_messages`; pictures are copied into the `ig-media` bucket
because Meta's CDN links expire within hours.

Instagram has no API event for "moved to the General folder", so the order
signal is a message *we* send: when staff reply with the saved reply that
contains the trigger phrase (IG_ORDER_TRIGGER, default "#order"), Meta echoes
that message to us (is_echo), and this function hands the whole thread to
Claude, which returns the order as JSON → `ig_orders`. Staff see it in the
admin page under "Aura orders".

Routes
  GET  ?hub.mode=subscribe&hub.verify_token=…&hub.challenge=…  Meta's verification
  GET  (anything else)                 health: which env vars are set (no values)
  GET  ?extract=<igsid>                re-run the extraction for one customer;
                                       needs a staff Supabase session
                                       (Authorization: Bearer <access token>)
  POST                                 the webhook

Environment (set in Vercel → Project → Settings → Environment Variables)
  IG_VERIFY_TOKEN        any string; the same string goes into Meta's "Verify token"
  IG_APP_SECRET          Meta app → App settings → Basic → App secret
  IG_ACCESS_TOKEN        the Instagram user token generated for aura.originall
  IG_ORDER_TRIGGER       optional; phrases separated by "|" (default "#order")
  SUPABASE_URL           https://….supabase.co
  SUPABASE_SERVICE_KEY   Supabase → Project settings → API → service_role (secret)
  ANTHROPIC_API_KEY      console.anthropic.com
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

GRAPH = "https://graph.instagram.com/v23.0"
BUCKET = "ig-media"
MODEL = "claude-opus-5-5"
MAX_IMAGES = 20          # pictures handed to Claude per extraction
THREAD_DAYS = 30         # how far back a thread is read when there is no earlier order
THREAD_LIMIT = 150
BACKFILL_MAX = int(os.environ.get("IG_BACKFILL_MAX") or 120)   # Meta documents 20 but returned 48+ in practice

ENV_KEYS = ["IG_VERIFY_TOKEN", "IG_APP_SECRET", "IG_ACCESS_TOKEN", "IG_ORDER_TRIGGER",
            "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "ANTHROPIC_API_KEY"]


def env(k, default=""):
    return (os.environ.get(k) or default).strip()


def triggers():
    return [p.strip() for p in env("IG_ORDER_TRIGGER", "#order").split("|") if p.strip()]


# ---------------------------------------------------------------- Supabase
def sb_headers(extra=None):
    key = env("SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def sb(method, path, body=None, headers=None, timeout=20):
    """One REST call against Supabase; returns the decoded JSON (or None)."""
    url = env("SUPABASE_URL").rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=sb_headers(headers))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def sb_insert(table, rows, on_conflict=None):
    """Insert rows; with on_conflict, duplicates are ignored and the inserted
    rows (only those) come back, so the caller can tell a retry apart."""
    q = "?on_conflict=" + on_conflict if on_conflict else ""
    prefer = "return=representation" + (",resolution=ignore-duplicates" if on_conflict else "")
    return sb("POST", "/rest/v1/" + table + q, rows, {"Prefer": prefer}) or []


def sb_select(table, params):
    return sb("GET", "/rest/v1/" + table + "?" + urllib.parse.urlencode(params)) or []


def sb_upload(path, data, content_type):
    url = env("SUPABASE_URL").rstrip("/") + "/storage/v1/object/" + BUCKET + "/" + path
    h = sb_headers({"Content-Type": content_type, "x-upsert": "true"})
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    return env("SUPABASE_URL").rstrip("/") + "/storage/v1/object/public/" + BUCKET + "/" + path


def staff_from_jwt(auth_header):
    """Admin page calls carry the staff member's Supabase session; Supabase
    tells us who it is, and the staff table says whether they count."""
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header[7:].strip()
    req = urllib.request.Request(env("SUPABASE_URL").rstrip("/") + "/auth/v1/user",
                                 headers={"apikey": env("SUPABASE_SERVICE_KEY"),
                                          "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            user = json.loads(r.read())
    except urllib.error.HTTPError:
        return None
    rows = sb_select("staff", {"select": "full_name", "id": "eq." + user.get("id", "")})
    return rows[0]["full_name"] if rows else None


# ---------------------------------------------------------------- Instagram
def ig_profile(igsid):
    """username/name for a customer; cached in ig_customers."""
    cached = sb_select("ig_customers", {"select": "username,name", "igsid": "eq." + igsid})
    if cached and cached[0].get("username"):
        return cached[0]
    prof = {"username": None, "name": None}
    try:
        q = urllib.parse.urlencode({"fields": "username,name", "access_token": env("IG_ACCESS_TOKEN")})
        with urllib.request.urlopen(GRAPH + "/" + igsid + "?" + q, timeout=15) as r:
            j = json.loads(r.read())
        prof = {"username": j.get("username"), "name": j.get("name")}
    except Exception:
        pass
    try:
        sb("POST", "/rest/v1/ig_customers?on_conflict=igsid",
           [{"igsid": igsid, **prof, "updated_at": now_iso()}],
           {"Prefer": "resolution=merge-duplicates,return=minimal"})
    except Exception:
        pass
    return prof


def copy_media(igsid, mid, i, url):
    """Download one attachment from Meta's CDN and keep it in our bucket.
    Returns the public URL, or None when it is not an image."""
    req = urllib.request.Request(url, headers={"User-Agent": "orderspo/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = r.read(8_000_000)
    if not ctype.startswith("image/"):
        return None
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(ctype, "jpg")
    safe = re.sub(r"[^A-Za-z0-9_-]", "", mid)[-40:] or "m"
    return sb_upload(f"{igsid}/{safe}-{i}.{ext}", data, ctype)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def ts_iso(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).isoformat()


def store_event(ev):
    """One `messaging` event → ig_messages row. Returns (row, inserted)."""
    msg = ev.get("message") or {}
    mid = msg.get("mid")
    if not mid or msg.get("is_deleted"):
        return None, False
    echo = bool(msg.get("is_echo"))
    igsid = ev["recipient"]["id"] if echo else ev["sender"]["id"]
    media = []
    for i, a in enumerate(msg.get("attachments") or []):
        payload = a.get("payload") or {}
        url = payload.get("url")
        item = {"type": a.get("type"), "url": url, "title": payload.get("title"), "stored_url": None}
        if url:
            try:
                item["stored_url"] = copy_media(igsid, mid, i, url)
            except Exception as e:  # keep the row even if the picture copy fails
                item["error"] = str(e)[:200]
        media.append(item)
    row = {"mid": mid, "igsid": igsid, "direction": "out" if echo else "in",
           "text": msg.get("text"), "media": media, "raw": ev, "sent_at": ts_iso(ev.get("timestamp", 0))}
    inserted = sb_insert("ig_messages", [row], on_conflict="mid")
    return row, bool(inserted)


def graph_get(path, params):
    params = dict(params, access_token=env("IG_ACCESS_TOKEN"))
    with urllib.request.urlopen(GRAPH + "/" + path + "?" + urllib.parse.urlencode(params), timeout=20) as r:
        return json.loads(r.read())


def igsid_for_username(username):
    """Find the customer's Instagram-scoped id by username among the
    account's conversations (newest first, a few pages)."""
    want = username.lower().lstrip("@")
    params = {"platform": "instagram", "fields": "participants", "limit": "50"}
    path = "me/conversations"
    for _ in range(8):
        page = graph_get(path, params)
        for conv in page.get("data") or []:
            for p in (conv.get("participants") or {}).get("data") or []:
                if (p.get("username") or "").lower() == want:
                    return p.get("id")
        nxt = (page.get("paging") or {}).get("cursors", {}).get("after")
        if not nxt:
            return None
        params["after"] = nxt
    return None


def backfill_thread(igsid):
    """Chats that existed before the webhook went live were never delivered
    to us. Meta's API returns the last 20 messages of a conversation with
    their content, so we pull those and store them like webhook events
    (same message ids, so nothing is stored twice)."""
    try:
        conv = graph_get("me/conversations", {"user_id": igsid, "platform": "instagram", "fields": "participants"})
        first = (conv.get("data") or [{}])[0]
        cid = first.get("id")
        if not cid:
            return 0
        # the participant list is the reliable source of the username
        for p in (first.get("participants") or {}).get("data") or []:
            if p.get("id") == igsid and p.get("username"):
                sb("POST", "/rest/v1/ig_customers?on_conflict=igsid",
                   [{"igsid": igsid, "username": p["username"], "name": p.get("name"), "updated_at": now_iso()}],
                   {"Prefer": "resolution=merge-duplicates,return=minimal"})
        msgs, params = [], {"limit": "50"}
        while len(msgs) < BACKFILL_MAX:           # Meta pages the list (48 per page seen)
            page = graph_get(cid + "/messages", params)
            msgs += page.get("data") or []
            after = ((page.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not (page.get("paging") or {}).get("next"):
                break
            params["after"] = after
    except Exception:
        traceback.print_exc()
        return 0

    # messages already stored (webhook or an earlier backfill) are skipped
    try:
        have = {r["mid"] for r in sb_select("ig_messages", {"select": "mid", "igsid": "eq." + igsid, "limit": "2000"})}
    except Exception:
        have = set()
    todo = [m for m in msgs[:BACKFILL_MAX] if m.get("id") not in have]

    def detail(m):
        try:
            d = graph_get(m["id"], {"fields": "id,created_time,from,to,message,attachments,shares"})
        except Exception:
            return None
        # copy pictures inside the worker so downloads run in parallel too
        d["_media"] = []
        for i, a in enumerate((d.get("attachments") or {}).get("data") or []):
            url = ((a.get("image_data") or {}).get("url") or (a.get("video_data") or {}).get("url")
                   or a.get("file_url"))
            item = {"type": "image" if a.get("image_data") else ("video" if a.get("video_data") else "file"),
                    "url": url, "title": a.get("name"), "stored_url": None}
            if url:
                try:
                    item["stored_url"] = copy_media(igsid, d["id"], i, url)
                except Exception as e:
                    item["error"] = str(e)[:200]
            d["_media"].append(item)
        return d
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        details = list(pool.map(detail, todo))
    added = 0
    for d in details:
        if not d or not d.get("created_time"):
            continue
        sender = (d.get("from") or {}).get("id")
        direction = "in" if sender == igsid else "out"
        media = d["_media"]
        for sh in (d.get("shares") or {}).get("data") or []:
            media.append({"type": "share", "url": sh.get("link"), "title": sh.get("name"), "stored_url": None})
        ts = dt.datetime.strptime(d["created_time"], "%Y-%m-%dT%H:%M:%S%z").isoformat()
        row = {"mid": d["id"], "igsid": igsid, "direction": direction, "text": d.get("message"),
               "media": media, "raw": {"backfill": True}, "sent_at": ts}
        try:
            added += len(sb_insert("ig_messages", [row], on_conflict="mid"))
        except Exception:
            traceback.print_exc()
    return added


# ---------------------------------------------------------------- Claude
ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "customer_name": {"type": ["string", "null"]},
        "phone": {"type": ["string", "null"], "description": "Iraqi number as written, digits only, e.g. 07501234567"},
        "city": {"type": ["string", "null"]},
        "address": {"type": ["string", "null"], "description": "delivery address / nearest landmark, as the customer wrote it"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "variant": {"type": ["string", "null"], "description": "shade, colour, size or number if stated"},
                    "qty": {"type": "integer"},
                    "unit_price_iqd": {"type": ["integer", "null"]},
                    "image_no": {"type": ["integer", "null"], "description": "number of the picture that shows this product, or null"},
                    "note": {"type": ["string", "null"]}
                },
                "required": ["name", "variant", "qty", "unit_price_iqd", "image_no", "note"],
                "additionalProperties": False
            }
        },
        "total_iqd": {"type": ["integer", "null"]},
        "delivery_iqd": {"type": ["integer", "null"]},
        "notes": {"type": ["string", "null"], "description": "anything staff should know: ambiguities, assumptions, special requests"},
        "missing": {"type": "array", "items": {"type": "string"}, "description": "which of name/phone/address/items/price could not be found"}
    },
    "required": ["customer_name", "phone", "city", "address", "items", "total_iqd", "delivery_iqd", "notes", "missing"],
    "additionalProperties": False
}

SYSTEM = """You read an Instagram DM conversation between a shop and one customer and write down the order.

The shop is "Aura Original" in Iraq (Kurdistan region); it sells SHEGLAM make-up and beauty products. Most customers write Kurdish (Sorani) in Latin letters, with no fixed spelling: "slaw", "amam dawaya" / "ama dmawe" (I want this), "nawm Roza ya" (my name is Roza), "zhmaram" / "jmara" (my phone), "nawnishan" / "naw nishan" (address), "Hawler" = Erbil, "Slemani" = Sulaymaniyah, "Dhok" = Duhok, "zhmara" can also mean a shade number, "danayek" = one piece, "du dana" = two, "chand a?" = how much. Others write Kurdish in Arabic script, Arabic, or English, often mixed, often with Arabic-Indic digits. Prices are Iraqi dinar: "25", "٢٥", "25 hazar" or "25 هەزار" in a price context means 25,000 IQD. Delivery is usually 5,000 IQD but only record it if it is mentioned. Keep names, phone numbers and addresses exactly as the customer wrote them (same script, no transliteration). The transcript shows who wrote each line (CUSTOMER or SHOP) and numbers the pictures; the pictures follow the transcript in the same numbering.

Write every free-text field in Kurdish Sorani with Arabic script: item names, variants, item notes, notes and the missing list. For a product, give the Kurdish description first and then the brand and English product name in brackets so staff can find it in the SHEIN app, e.g. "ماسکارای دوو سەرە (SHEGLAM Double-Ended Mascara)". For missing use the words ناو, ژمارەی مۆبایل, ناونیشان, کاڵاکان, نرخ.

Record only what the conversation supports. Products are often shown as pictures or shared posts rather than named: then name the product from the picture (brand, product type, shade if readable) and point to the picture number. Prices quoted by SHOP are the prices that count. If the customer changed their mind, record the final state. Address and phone are whatever the customer sent last. Put anything unclear in notes rather than guessing, and list truly absent fields in missing."""


def load_thread(igsid, until_iso):
    prior = sb_select("ig_orders", {"select": "to_ts", "igsid": "eq." + igsid,
                                    "order": "created_at.desc", "limit": "1"})
    since = prior[0]["to_ts"] if prior and prior[0].get("to_ts") else \
        (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=THREAD_DAYS)).isoformat()
    params = {"select": "mid,direction,text,media,sent_at", "igsid": "eq." + igsid,
              "sent_at": "gt." + since, "order": "sent_at.asc", "limit": str(THREAD_LIMIT)}
    rows = sb_select("ig_messages", params)
    rows = [r for r in rows if r["sent_at"] <= until_iso] if until_iso else rows
    return rows


def build_content(rows):
    """Transcript text + numbered image blocks for Claude."""
    lines, images = [], []
    for r in rows:
        who = "SHOP" if r["direction"] == "out" else "CUSTOMER"
        when = r["sent_at"][:16].replace("T", " ")
        parts = []
        if r.get("text"):
            parts.append(r["text"])
        for m in r.get("media") or []:
            if m.get("stored_url"):
                images.append({"no": len(images) + 1, "url": m["stored_url"], "who": who})
                parts.append(f"[picture {len(images)}]")
            else:
                t = m.get("type") or "attachment"
                parts.append(f"[{t}{': ' + m['title'] if m.get('title') else ''}]")
        lines.append(f"{when} {who}: " + " ".join(parts))
    if len(images) > MAX_IMAGES:        # keep the most recent pictures, renumber nothing
        drop = {im["no"] for im in images[:-MAX_IMAGES]}
        images = [im for im in images if im["no"] not in drop]
    content = [{"type": "text", "text": "Conversation:\n\n" + "\n".join(lines)}]
    for im in images:
        content.append({"type": "text", "text": f"picture {im['no']} (sent by {im['who']}):"})
        content.append({"type": "image", "source": {"type": "url", "url": im["url"]}})
    content.append({"type": "text", "text": "Write down the order from this conversation."})
    return content, images


def ask_claude(content):
    import anthropic
    client = anthropic.Anthropic(timeout=50, max_retries=1)
    kw = dict(model=MODEL, max_tokens=4096, system=SYSTEM,
              output_config={"effort": "high", "format": {"type": "json_schema", "schema": ORDER_SCHEMA}},
              messages=[{"role": "user", "content": content}])
    try:
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                           extra_body={"fallbacks": "default"}, **kw)
    except anthropic.BadRequestError:
        resp = client.messages.create(**kw)
    if resp.stop_reason == "refusal":
        return None, "Claude declined to read this conversation.", resp.model
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text), None, resp.model
    except ValueError:
        return None, "Claude did not return JSON: " + text[:300], resp.model


def extract_order(igsid, trigger_mid, until_iso=None, by=None):
    backfill_thread(igsid)
    rows = load_thread(igsid, until_iso)
    if not rows:
        return {"ok": False, "error": "no messages for this customer"}
    prof = ig_profile(igsid)
    content, images = build_content(rows)
    data, err, model = ask_claude(content)
    by_no = {im["no"]: im["url"] for im in images}
    items = []
    if data:
        for it in data.get("items") or []:
            items.append({"name": it.get("name"), "variant": it.get("variant"), "qty": it.get("qty") or 1,
                          "unit_price_iqd": it.get("unit_price_iqd"),
                          "image_url": by_no.get(it.get("image_no")), "note": it.get("note")})
    order = {
        "igsid": igsid, "username": prof.get("username"),
        "customer_name": data.get("customer_name") if data else None,
        "phone": data.get("phone") if data else None,
        "city": data.get("city") if data else None,
        "address": data.get("address") if data else None,
        "items": items,
        "total_iqd": data.get("total_iqd") if data else None,
        "delivery_iqd": data.get("delivery_iqd") if data else None,
        "notes": (data.get("notes") if data else None) or err,
        "missing": (data.get("missing") if data else None) or (["all"] if err else []),
        "trigger_mid": trigger_mid, "from_ts": rows[0]["sent_at"], "to_ts": rows[-1]["sent_at"],
        "model": model, "updated_by": by,
    }
    saved = sb_insert("ig_orders", [order], on_conflict="trigger_mid")
    return {"ok": True, "order": saved[0] if saved else order, "duplicate": not saved}


# ---------------------------------------------------------------- HTTP
class handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # Vercel collects stdout; keep the default noise out
        pass

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        g = lambda k: (q.get(k) or [""])[0]
        if g("hub.mode") == "subscribe":
            if env("IG_VERIFY_TOKEN") and hmac.compare_digest(g("hub.verify_token"), env("IG_VERIFY_TOKEN")):
                return self._send(200, g("hub.challenge"), "text/plain")
            return self._send(403, "verify token mismatch", "text/plain")
        if g("probe"):
            who = staff_from_jwt(self.headers.get("Authorization") or "")
            if not who:
                return self._send(401, {"ok": False, "error": "staff session required"})
            try:
                conv = graph_get("me/conversations", {"user_id": g("probe"), "platform": "instagram"})
                cid = (conv.get("data") or [{}])[0].get("id")
                mm = graph_get(cid, {"fields": "messages.limit(100)"}).get("messages", {})
                msgs = mm.get("data") or []
                out = [{"paging": bool((mm.get("paging") or {}).get("next"))}]
                for m in msgs[:100]:
                    try:
                        d = graph_get(m["id"], {"fields": "created_time,message,attachments,shares"})
                        out.append({"t": d.get("created_time"), "txt": (d.get("message") or "")[:20],
                                    "att": len((d.get("attachments") or {}).get("data") or []),
                                    "sh": len((d.get("shares") or {}).get("data") or [])})
                    except urllib.error.HTTPError as e:
                        out.append({"err": e.code})
                return self._send(200, {"ok": True, "n": len(msgs), "msgs": out})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)[:300]})
        if g("extract"):
            who = staff_from_jwt(self.headers.get("Authorization") or "")
            if not who:
                return self._send(401, {"ok": False, "error": "staff session required"})
            try:
                target = g("extract").strip()
                if target.startswith("@") or not target.isdigit():
                    igsid = igsid_for_username(target)
                    if not igsid:
                        return self._send(404, {"ok": False, "error": "no chat with @" + target.lstrip("@")})
                    target = igsid
                res = extract_order(target, "manual-" + now_iso(), by=who)
            except Exception as e:
                traceback.print_exc()
                res = {"ok": False, "error": str(e)[:500]}
            return self._send(200 if res.get("ok") else 500, res)
        return self._send(200, {"ok": True, "env": {k: bool(env(k)) for k in ENV_KEYS},
                                "trigger": triggers(), "model": MODEL})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        secret = env("IG_APP_SECRET")
        sig = self.headers.get("X-Hub-Signature-256") or ""
        if not secret:
            return self._send(500, "IG_APP_SECRET not set", "text/plain")
        want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return self._send(403, "bad signature", "text/plain")
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return self._send(400, "bad json", "text/plain")

        stored, orders = 0, []
        try:
            for entry in payload.get("entry") or []:
                for ev in entry.get("messaging") or []:
                    if "message" not in ev:
                        continue                      # reads, reactions, postbacks
                    row, inserted = store_event(ev)
                    if not row:
                        continue
                    stored += int(inserted)
                    text = row.get("text") or ""
                    if inserted and row["direction"] == "out" and any(p in text for p in triggers()):
                        orders.append(extract_order(row["igsid"], row["mid"], row["sent_at"]))
        except Exception as e:
            # Meta retries on non-2xx, which would re-run everything stored so
            # far; the mid unique key makes that safe, so let it retry.
            traceback.print_exc()
            return self._send(500, {"ok": False, "error": str(e)[:500]})
        return self._send(200, {"ok": True, "stored": stored, "orders": len(orders)})
