const CACHE_NAME = 'namma-models-v1';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin
      || !url.pathname.startsWith('/scene/') || !/\.(ply|glb|spz)$/i.test(url.pathname)
      || !url.searchParams.has('v')) return;

  // Stream the first download to the viewer while saving a separate copy.
  // Unlike the HTTP cache, this explicitly retains each completed model.
  const result = (async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(event.request);
    if (cached) return { response: cached, saved: Promise.resolve() };
    const response = await fetch(event.request);
    if (!response.ok) return { response, saved: Promise.resolve() };
    const saved = cache.put(event.request, response.clone()).then(async () => {
      for (const key of await cache.keys()) {
        const old = new URL(key.url);
        if (old.pathname === url.pathname && old.searchParams.get('scene') === url.searchParams.get('scene')
            && key.url !== event.request.url) await cache.delete(key);
      }
    }).catch((error) => {
      console.warn('Could not retain model in browser storage:', error);
    });
    return { response, saved };
  })();
  event.respondWith(result.then(({ response }) => response));
  event.waitUntil(result.then(({ saved }) => saved));
});
