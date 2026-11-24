/* The shell caches; the data never does.
 *
 * The app is online-only by design — one server owns the database — so this
 * worker exists to make launch instant and to survive a flaky tunnel, not to
 * serve stale tasks. Anything under /rpc, /events or /v1 goes straight to the
 * network and is never stored.
 */

const CACHE = "omagtd-shell-v7";
const SHELL = [
  "/",
  "/style.css?v=7",
  "/app.js?v=7",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/rpc") ||
      url.pathname.startsWith("/events") ||
      url.pathname.startsWith("/v1/")) {
    return;
  }

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
        }
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("/")))
  );
});
