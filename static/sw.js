/*
 * Minimal service worker.
 *
 * 1. Precache the shell on install: index.html, login.html, manifest.
 * 2. Pass-through every fetch — no offline strategy yet, the PWA
 *    needs the network to talk to the backend.
 * 3. On a 401 from any /feed, /tags, /search, /ingest, /files
 *    request, redirect to /login.html so the user can re-auth.
 */
const SHELL = ["/", "/login.html", "/manifest.webmanifest"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open("lila-shell-v1").then((c) => c.addAll(SHELL)));
});
self.addEventListener("fetch", (event) => {
  event.respondWith(
    fetch(event.request).then((r) => {
      if (r.status === 401 && event.request.mode === "navigate") {
        return Response.redirect("/login.html", 302);
      }
      return r;
    }).catch(() => caches.match(event.request))
  );
});
