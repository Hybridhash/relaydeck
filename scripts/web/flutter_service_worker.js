/* RelayDeck pinned-shell service worker.
 * Generated into the build by scripts/web_service_worker.py from this template; edit the template.
 * One worker per build: the cache is named for the BUILD digest, so a build never mixes files
 * with another build's shell. Replaces Flet's stub at the same URL. No skipWaiting and no
 * clients.claim: a new build waits until all RelayDeck tabs are closed.
 */
'use strict';

const BUILD = '__BUILD__';
const PREFIX = 'relaydeck-shell-';
const CACHE = PREFIX + BUILD;
const SHELL = __SHELL__;
// SHA-256 per runtime-cacheable path (CanvasKit, fonts). A runtime fetch is stored only when its
// bytes match; a path not listed is served from the network and never cached.
const RUNTIME_HASHES = __RUNTIME_HASHES__;
const SHELL_PATHS = new Set(SHELL.map((name) => '/' + name));
const NAVIGATION = '/';
const NAV_TIMEOUT_MS = 4000;
// Cached at runtime on first fetch (cache-first), into this build's cache only: the Noto fallback
// fonts, and the CanvasKit variant this browser loads (iPhone and Chrome each store their own).
const RUNTIME_PREFIXES = ['/assets/fonts/', '/canvaskit/'];

// Expected content types per extension: a server's HTML fallback is never cached as a file.
const TYPES = {
  '.js': ['text/javascript', 'application/javascript'],
  '.wasm': ['application/wasm'],
  '.json': ['application/json'],
  '.png': ['image/png'],
  '.ico': ['image/'],
  '.otf': ['font/', 'application/'],
  '.bin': ['application/octet-stream', 'application/'],
  '.woff2': ['font/', 'application/'],
};

// Never intercepted: the API, auth and recovery, WebSocket, Python runtime, the app package,
// this worker, and the kill switch.
function passesThrough(url) {
  const path = url.pathname;
  return path === '/ws' || path.startsWith('/v1/') || path.startsWith('/pyodide/') ||
    path === '/python-worker.js' || path.startsWith('/assets/app/') ||
    path === '/flutter_service_worker.js' || path === '/rd-sw-reset.html';
}

function contentTypeOk(response, path) {
  const dot = path.lastIndexOf('.');
  const expected = TYPES[dot < 0 ? '' : path.slice(dot).toLowerCase()];
  if (!expected) return true;
  const type = (response.headers.get('content-type') || '').toLowerCase();
  return expected.some((prefix) => type.startsWith(prefix));
}

function isHtml(response) {
  return (response.headers.get('content-type') || '').toLowerCase().startsWith('text/html');
}

// The build id the served page declares (its rd-build meta), or '' when absent.
function buildOf(html) {
  const match = /<meta name="rd-build" content="([^"]+)"/.exec(html);
  return match ? match[1] : '';
}

function withTimeout(promise, ms) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('timeout')), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

// Install: precache the served page and every shell file, all or nothing. 'no-cache' revalidates
// with the server, so an unchanged file costs a 304 and reuses the browser's HTTP-cache body
// (no re-download). A failure (status, content type, another build's page, or quota) deletes this
// build's cache, so the old worker stays in control.
async function precache() {
  try {
    const cache = await caches.open(CACHE);
    const page = await fetch(NAVIGATION, { cache: 'no-cache', redirect: 'error' });
    if (!page.ok || !isHtml(page) || buildOf(await page.clone().text()) !== BUILD) {
      throw new Error('served page is not this build');
    }
    await cache.put(NAVIGATION, page);
    for (const path of SHELL_PATHS) {
      const response = await fetch(path, { cache: 'no-cache', redirect: 'error' });
      if (!response.ok || !contentTypeOk(response, path)) throw new Error('shell fetch: ' + path);
      await cache.put(path, response);
    }
  } catch (error) {
    await caches.delete(CACHE).catch(() => {});
    throw error;
  }
}

self.addEventListener('install', (event) => {
  event.waitUntil(precache());
});

// Activate: delete only other RelayDeck shell caches, never another origin's or app's caches.
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith(PREFIX) && key !== CACHE) await caches.delete(key);
    }
  })());
});

// Navigation: network-first with a timeout (the relay can be slow), then this build's shell.
// A page that declares another build is not served, so one tab never mixes two builds.
// Only the bare root (no query) is stored, so no credentials-bearing URL is cached.
async function navigation(request, url) {
  const cache = await caches.open(CACHE);
  try {
    const page = await withTimeout(fetch(request), NAV_TIMEOUT_MS);
    if (page.ok && !page.redirected && isHtml(page)) {
      if (buildOf(await page.clone().text()) === BUILD) {
        if (url.pathname === NAVIGATION && url.search === '') await cache.put(NAVIGATION, page.clone());
        return page;
      }
    }
  } catch (_) {
    // Timeout or offline: fall back to the pinned shell below.
  }
  const shell = await cache.match(NAVIGATION);
  return shell || fetch(request);
}

async function sha256Hex(buffer) {
  const digest = await crypto.subtle.digest('SHA-256', buffer);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}

// Shell files and runtime files (fonts, CanvasKit): cache-first within this build's cache. A miss
// is stored only in this build's cache, and a runtime file only when its bytes match RUNTIME_HASHES.
// Otherwise the network response is returned without caching.
async function cachedFile(request, path) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(path);
  if (hit) return hit;
  const response = await fetch(request);
  if (!response.ok || !contentTypeOk(response, path)) return response;
  if (!SHELL_PATHS.has(path)) {
    const expected = RUNTIME_HASHES[path];
    if (!expected) return response;
    const hash = await sha256Hex(await response.clone().arrayBuffer());
    if (hash !== expected) return response;
  }
  await cache.put(path, response.clone());
  return response;
}

// The page asks which build this worker is, and asks to activate it. Activation
// happens only for a valid request from the one RelayDeck window that is open (the registration is
// shared by every window, so an activation must not leave another window on the old build).
// No clients.claim: the new build takes over at the next navigation, or when the page reloads.
const NONCE = /^[0-9a-f]{32}$/;

async function activateIfOnlyWindow(source, nonce, reply) {
  const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  const requester = source && source.id;
  const onlyThisWindow = windows.length === 1 && requester !== undefined && windows[0].id === requester;
  if (!onlyThisWindow) {
    reply({ type: 'RD_ACTIVATE_REFUSED', reason: 'other_windows', count: windows.length });
    return;
  }
  reply({ type: 'RD_ACTIVATE_ACCEPTED', nonce });
  await self.skipWaiting();
}

self.addEventListener('message', (event) => {
  const data = event.data;
  const port = event.ports && event.ports[0];
  const reply = (message) => {
    if (port) port.postMessage(message);
    else if (event.source && typeof event.source.postMessage === 'function') event.source.postMessage(message);
  };
  if (!data || typeof data !== 'object') return;
  if (data.type === 'RD_BUILD?') {
    reply({ type: 'RD_BUILD', build: BUILD });
    return;
  }
  if (data.type !== 'RD_ACTIVATE') return;
  if (data.build !== BUILD || typeof data.nonce !== 'string' || !NONCE.test(data.nonce)) {
    reply({ type: 'RD_ACTIVATE_REFUSED', reason: 'bad_request' });
    return;
  }
  event.waitUntil(activateIfOnlyWindow(event.source, data.nonce, reply));
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || passesThrough(url)) return;
  if (request.mode === 'navigate') {
    event.respondWith(navigation(request, url));
    return;
  }
  if (SHELL_PATHS.has(url.pathname) || RUNTIME_PREFIXES.some((p) => url.pathname.startsWith(p))) {
    event.respondWith(cachedFile(request, url.pathname));
  }
});

// A push shows only the generic text the server sent (or a fixed fallback). The payload
// never carries terminal text, and neither the push nor the click authorizes any terminal action.
// The click opens the app; the route token is only a hint the app re-checks with the server.
const ROUTE_PATTERN = /^wp_[A-Za-z0-9_-]{20,64}$/;
const FALLBACK_BODY = 'An agent needs attention';

async function showPush(data) {
  let doc = null;
  try { doc = data ? data.json() : null; } catch (_) { doc = null; }
  const body = doc && typeof doc.b === 'string' && doc.b.length <= 120 ? doc.b : FALLBACK_BODY;
  const route = doc && typeof doc.r === 'string' && ROUTE_PATTERN.test(doc.r) ? doc.r : '';
  await self.registration.showNotification('RelayDeck', {
    body,
    tag: route || 'relaydeck',
    data: { route },
  });
}

self.addEventListener('push', (event) => {
  event.waitUntil(showPush(event.data));
});

function routeTarget(route) {
  return typeof route === 'string' && ROUTE_PATTERN.test(route)
    ? '/?rd_route=' + encodeURIComponent(route)
    : '/';
}

// Focus only a same-origin window; otherwise open the app. Never a cross-origin URL.
async function openApp(path) {
  const target = new URL(path, self.location.origin);
  if (target.origin !== self.location.origin) return;
  const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  const own = windows.find((client) => new URL(client.url).origin === self.location.origin);
  if (own) {
    await own.focus();
    return;
  }
  await self.clients.openWindow(target.href);
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  event.waitUntil(openApp(routeTarget(data.route)));
});
