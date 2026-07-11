// Automation Studio Service Worker v3 (network-first)
// 過去版のキャッシュを破棄し、HTML と manifest は常にネットワーク優先とする。
// 静的資産（/static/*）のみキャッシュ（stale-while-revalidate）。

const CACHE_NAME = 'automation-studio-static-v4';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      // 古いキャッシュは全削除
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
      await self.clients.claim();
    })()
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // API / WebSocket / 動的: 常にネットワーク（SW はスルー）
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) return;
  // HTML ページ / manifest / sw 自身は network-first
  if (event.request.mode === 'navigate' ||
      url.pathname === '/' ||
      url.pathname.endsWith('.html') ||
      url.pathname === '/manifest.json' ||
      url.pathname === '/sw.js') {
    return; // ブラウザ既定（常に network）に委ねる
  }
  // /static/* のみ stale-while-revalidate
  if (event.request.method === 'GET' && url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.open(CACHE_NAME).then(async (cache) => {
        const cached = await cache.match(event.request);
        const fetchPromise = fetch(event.request).then((res) => {
          if (res && res.status === 200) cache.put(event.request, res.clone());
          return res;
        }).catch(() => cached);
        return cached || fetchPromise;
      })
    );
  }
});
