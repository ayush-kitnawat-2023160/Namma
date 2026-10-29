let ready;

export function prepareModelCache() {
  if (import.meta.env.VITE_STATIC_DEPLOY === '1') return Promise.resolve(false);
  if (ready) return ready;
  ready = (async () => {
    if (!('serviceWorker' in navigator)) return false;
    try {
      await navigator.serviceWorker.register('/viewer/model-cache-sw.js');
      await navigator.serviceWorker.ready;
      if (!navigator.serviceWorker.controller) {
        await new Promise((resolve) => {
          navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true });
        });
      }
      return true;
    } catch (error) {
      console.warn('Model cache unavailable:', error);
      return false;
    }
  })();
  return ready;
}

export async function modelIsCached(url) {
  if (!await prepareModelCache()) return false;
  try {
    const cache = await caches.open('namma-models-v1');
    return Boolean(await cache.match(url));
  } catch {
    return false;
  }
}
