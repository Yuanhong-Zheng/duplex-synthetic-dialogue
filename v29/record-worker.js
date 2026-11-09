/* Serve the frozen record files from content-preserving, path-hashed JSON shards. */
const shardCache = new Map();
const maxCachedShards = 8;

self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

async function recordResponse(url) {
  const prefix = new URL('records/', self.registration.scope).pathname;
  let path;
  try {
    path = 'records/' + decodeURIComponent(url.pathname.slice(prefix.length));
  } catch (_) {
    return new Response('Invalid record path', {status: 400});
  }
  try {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(path));
    const shard = Array.from(new Uint8Array(digest).slice(0, 1),
      byte => byte.toString(16).padStart(2, '0')).join('');
    let data = shardCache.get(shard);
    if (!data) {
      data = fetch('https://raw.githubusercontent.com/Yuanhong-Zheng/duplex-synthetic-dialogue/8f8b1763e011c6b048935b8db8c38dfc6adfa230/v29/shards/' + shard + '.json')
        .then(response => {
          if (!response.ok) throw new Error('Record shard unavailable: ' + response.status);
          return response.json();
        });
      shardCache.set(shard, data);
      if (shardCache.size > maxCachedShards) shardCache.delete(shardCache.keys().next().value);
      data.catch(() => { if (shardCache.get(shard) === data) shardCache.delete(shard); });
    }
    const records = await data;
    if (!Object.prototype.hasOwnProperty.call(records, path))
      return new Response('Record not found', {status: 404});
    const contentType = path.endsWith('.json') ? 'application/json; charset=utf-8'
      : 'text/plain; charset=utf-8';
    return new Response(records[path], {status: 200, headers: {'Content-Type': contentType}});
  } catch (_) {
    return new Response('记录加载失败，可返回案例页下载完整流程资料。', {
      status: 502, headers: {'Content-Type': 'text/plain; charset=utf-8'}
    });
  }
}

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  const scope = new URL(self.registration.scope);
  const prefix = new URL('records/', scope).pathname;
  if (url.origin === scope.origin && url.pathname.startsWith(prefix))
    event.respondWith(recordResponse(url));
});
