// Retired: the offline cache could leave phones stuck on an old or redirected copy of the app.
// This version clears the caches and unregisters itself; the app no longer registers a worker.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil((async () => {
  for (const k of await caches.keys()) await caches.delete(k);
  await self.registration.unregister();
  for (const c of await self.clients.matchAll({ type: "window" })) c.navigate(c.url);
})()));
