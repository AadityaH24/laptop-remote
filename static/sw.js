/* Minimal offline shell. Network-first so a running laptop always wins.
   Everything is derived from the registration scope, so the app works at any
   base path (e.g. /laptop-remote/). */
const SCOPE = self.registration.scope;
const BASE = new URL(SCOPE).pathname;
const SHELL = "lr-shell-v12";
const ASSETS = [
  SCOPE,
  SCOPE + "static/style.css?v=12",
  SCOPE + "static/app.js?v=12",
  SCOPE + "static/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith(BASE + "api/") || url.pathname === BASE + "ws") return;

  event.respondWith(
    fetch(req)
      .then((res) => {
        const copy = res.clone();
        caches.open(SHELL).then((c) => c.put(req, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(req).then((hit) => hit || caches.match(SCOPE)))
  );
});
