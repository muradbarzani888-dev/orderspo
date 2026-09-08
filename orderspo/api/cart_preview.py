"""Cart-share preview for admin (v110).

A SHEIN cart share link (onelink.shein.com/…) only opens inside the SHEIN app;
in a browser it bounces to the SHEIN homepage. The one thing the link exposes
publicly is its Open Graph metadata: a collage image of the cart items with the
item count stamped on it, plus a title. The admin page cannot read that itself
(no CORS on SHEIN's side), so this tiny Vercel function fetches the link
server-side and returns just the metadata as JSON.

GET /api/cart_preview?u=<cart link>
  -> {"image": "...jpg", "title": "...", "group_id": "862693680",
      "cart_share": true, "final_url": "..."}
Only shein.com hosts are fetched, so this is not an open proxy.
"""
import json
import re
import urllib.parse
import urllib.request
from html import unescape
from http.server import BaseHTTPRequestHandler

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
MAX_BYTES = 400_000


def allowed(url):
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and (host == "shein.com" or host.endswith(".shein.com"))


def meta(html, prop):
    # <meta property="og:image" content="..."/> — attribute order varies
    m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']*)["\']' % re.escape(prop), html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(prop), html, re.I)
    return unescape(m.group(1)).strip() if m else None


def parse(html, final_url):
    out = {"image": meta(html, "og:image"), "title": meta(html, "og:title"),
           "group_id": None, "cart_share": False, "final_url": final_url}
    # <input id="deeplink" value="sheinlink://applink/share_receiver?data=%7B...%7D">
    m = re.search(r'id=["\']deeplink["\'][^>]*value=["\']([^"\']+)["\']', html, re.I)
    if m:
        try:
            q = urllib.parse.urlsplit(unescape(m.group(1))).query
            data = urllib.parse.parse_qs(q).get("data", [None])[0]
            if data:
                d = json.loads(urllib.parse.unquote(data))
                out["group_id"] = str(d.get("group_id")) if d.get("group_id") else None
                out["cart_share"] = str(d.get("cart_share")) == "1"
        except (ValueError, AttributeError):
            pass
    return out


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read(MAX_BYTES).decode("utf-8", "replace"), r.geturl()


class handler(BaseHTTPRequestHandler):
    def _send(self, code, body, cache="no-store"):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        url = (qs.get("u") or [""])[0].strip()
        if not allowed(url):
            return self._send(400, {"error": "not a shein.com link"})
        try:
            html, final_url = fetch(url)
        except Exception as e:  # network / HTTP error: tell the client, don't 500
            return self._send(502, {"error": "fetch failed", "detail": str(e)[:200]})
        out = parse(html, final_url)
        if not out["image"]:
            return self._send(404, {"error": "no preview on this page", "final_url": final_url})
        # a share link is a snapshot; its collage does not change, so let the CDN keep it a day
        return self._send(200, out, "public, s-maxage=86400, max-age=3600")
