// Orderspo app service worker (app v1).
// Pages and app files: network first, so every open shows the latest version;
// the saved copy is used only when the phone is offline.
// Never touched: /admin, /api, Supabase, and anything that is not a GET.
const CACHE = "orderspo-app-v1";
const SHELL = [
  "/", "/order", "/app.js", "/manifest.webmanifest",
  "/icons/icon-192.png", "/icons/apple-touch-icon.png", "/icons/favicon-32.png"
];
const FONT_HOSTS = ["fonts.googleapis.com", "fonts.gstatic.com"];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k.startsWith("orderspo-") && k !== CACHE).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// One saved copy per page: "/?source=app" and "/" share the "/" copy.
function keyFor(url) { return url.origin + url.pathname; }

self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    if (url.pathname.startsWith("/admin") || url.pathname.startsWith("/api")) return;
    event.respondWith(
      fetch(req)
        .then(res => {
          // Redirected responses cannot be replayed for a page load in Safari.
          if (res.ok && res.type === "basic" && !res.redirected) {
            const copy = res.clone();
            caches.open(CACHE).then(c => c.put(keyFor(url), copy));
          }
          return res;
        })
        .catch(() =>
          caches.match(keyFor(url)).then(hit =>
            hit || (req.mode === "navigate" ? caches.match(url.origin + "/") : Response.error())
          )
        )
    );
    return;
  }

  if (FONT_HOSTS.includes(url.hostname)) {
    // Fonts rarely change: answer from the saved copy, refresh it in the background.
    event.respondWith(
      caches.open(CACHE).then(cache =>
        cache.match(req).then(hit => {
          const fresh = fetch(req).then(res => { cache.put(req, res.clone()); return res; }).catch(() => hit);
          return hit || fresh;
        })
      )
    );
  }
  // Everything else, including Supabase, goes straight to the network.
});
