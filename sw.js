/* Minimal service worker: exists only because older Chrome versions require
   a fetch handler before offering PWA install. It caches nothing — an empty
   fetch listener leaves every request going straight to the network, so the
   dashboard can never go stale behind a cache. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(clients.claim()));
self.addEventListener("fetch", () => {});
