// RevenueOS web app: makes the app installable. Pages always come from the network (no stale data);
// when offline, a short message is shown instead of the browser's error page.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(() => new Response(
    "<meta name=viewport content='width=device-width'><p style='font-family:sans-serif;padding:24px'>You're offline. RevenueOS needs a connection; try again in a moment.</p>",
    { headers: { "Content-Type": "text/html" } })));
});
