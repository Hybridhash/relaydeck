// Behaviour of the rendered pinned-shell worker against fake Cache
// Storage and fake fetch. Usage: node sw_worker_browser.cjs <rendered flutter_service_worker.js>
// This checks the worker's logic, not a browser. No network, no real caches.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
// Bodies of the runtime-cacheable files, as the build wrote them (the hash map matches these).
const bodies = process.argv[3] ? JSON.parse(fs.readFileSync(process.argv[3], 'utf8')) : {};

// Objects made inside the VM context have another realm's prototype; compare plain copies.
const plain = (value) => JSON.parse(JSON.stringify(value));

function response({ status = 200, type = 'text/html; charset=utf-8', body = '', redirected = false } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    redirected,
    headers: { get: (name) => (name.toLowerCase() === 'content-type' ? type : null) },
    clone() { return this; },
    async text() { return body; },
    async arrayBuffer() { return new TextEncoder().encode(body).buffer; },
  };
}

function build() {
  const listeners = {};
  const caches = new Map();
  const fetches = [];
  let routes = {};
  let timeoutFires = false;
  let windows = [];
  const skips = { count: 0 };
  const shown = [];
  const opened = [];
  const self = {
    location: { origin: 'https://app.example' },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    clients: {
      matchAll: async () => windows,
      openWindow: async (url) => { opened.push(url); },
    },
    registration: {
      showNotification: async (title, options) => { shown.push({ title, options }); },
    },
    skipWaiting: async () => { skips.count += 1; },
  };
  const cacheStore = () => {
    const store = new Map();
    return {
      store,
      async match(key) { return store.get(key); },
      async put(key, value) { store.set(key, value); },
    };
  };
  const cacheStorage = {
    async open(name) {
      if (!caches.has(name)) caches.set(name, cacheStore());
      return caches.get(name);
    },
    async keys() { return [...caches.keys()]; },
    async delete(name) { return caches.delete(name); },
  };
  const fetchFake = async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input.url;
    fetches.push({ url, init });
    const path = new URL(url, 'https://app.example').pathname;
    const route = routes[path];
    if (route === undefined) return response({ status: 404, type: 'text/plain' });
    if (route === 'hang') return new Promise(() => {});
    if (route instanceof Error) throw route;
    return route;
  };
  const context = {
    self, caches: cacheStorage, fetch: fetchFake, URL, Promise, Error, console,
    crypto: require('node:crypto').webcrypto, TextEncoder, Uint8Array, RegExp,
    setTimeout: (fn) => { if (timeoutFires) fn(); return 1; },
    clearTimeout: () => {},
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  const names = vm.runInContext('({ BUILD, CACHE, PREFIX, SHELL })', context);
  return {
    names, listeners, caches, storage: cacheStorage, fetches, skips, shown, opened,
    setWindows(list) { windows = list; },
    setRoutes(next) { routes = next; },
    setTimeoutFires(value) { timeoutFires = value; },
  };
}

function fireFetch(env, { url, method = 'GET', mode = 'no-cors' }) {
  let respondWith = null;
  const event = {
    request: { url, method, mode },
    respondWith(promise) { respondWith = promise; },
  };
  env.listeners.fetch(event);
  return respondWith;
}

async function installEvent(env) {
  let pending = null;
  env.listeners.install({ waitUntil(promise) { pending = promise; } });
  return pending;
}

const shellRoutes = (env, buildId, overrides = {}) => {
  const routes = { '/': response({ body: `<head><meta name="rd-build" content="${buildId}"></head>` }) };
  for (const name of env.names.SHELL) {
    const type = name.endsWith('.js') ? 'text/javascript'
      : name.endsWith('.wasm') ? 'application/wasm'
      : name.endsWith('.json') ? 'application/json'
      : name.endsWith('.png') ? 'image/png'
      : name.endsWith('.otf') ? 'font/otf'
      : name.endsWith('.ico') ? 'image/x-icon'
      : 'application/octet-stream';
    routes['/' + name] = response({ type, body: 'x' });
  }
  return Object.assign(routes, overrides);
};

async function main() {
  // 1. Install precaches the served page and every shell file into this build's cache.
  {
    const env = build();
    env.setRoutes(shellRoutes(env, env.names.BUILD));
    await installEvent(env);
    const cache = env.caches.get(env.names.CACHE);
    assert.ok(cache, 'current build cache exists');
    assert.ok(cache.store.has('/'), 'served page precached');
    for (const name of env.names.SHELL) assert.ok(cache.store.has('/' + name), name);
    assert.ok(env.fetches.length > 0);
    for (const call of env.fetches) assert.equal(call.init.cache, 'no-cache', call.url);
    assert.equal(env.names.SHELL.some((name) => name.startsWith('canvaskit/')), false);
    assert.equal([...cache.store.keys()].some((key) => key.startsWith('/canvaskit/')), false);
    console.log('PASS: install precaches the page and every shell file with no-cache, no CanvasKit');
  }

  // 2. Install fails cleanly on a missing shell file: nothing is left behind.
  {
    const env = build();
    const routes = shellRoutes(env, env.names.BUILD);
    delete routes['/' + env.names.SHELL[0]];
    env.setRoutes(routes);
    await assert.rejects(installEvent(env));
    assert.equal(env.caches.has(env.names.CACHE), false, 'partial cache deleted');
    console.log('PASS: install fails cleanly on a missing shell file');
  }

  // 3. Install fails when the served page is another build (no mixed build).
  {
    const env = build();
    env.setRoutes(shellRoutes(env, 'another-build'));
    await assert.rejects(installEvent(env));
    assert.equal(env.caches.has(env.names.CACHE), false);
    console.log('PASS: install refuses a page from another build');
  }

  // 4. Install fails on a wrong content type (a server HTML fallback is never a shell file).
  {
    const env = build();
    const routes = shellRoutes(env, env.names.BUILD);
    routes['/' + env.names.SHELL[0]] = response({ type: 'text/html', body: '<html>' });
    env.setRoutes(routes);
    await assert.rejects(installEvent(env));
    console.log('PASS: install rejects a wrong content type');
  }

  // 5. A listed file is served from this build's cache without the network.
  {
    const env = build();
    const cache = await env.storage.open(env.names.CACHE);
    cache.store.set('/main.dart.js', response({ type: 'text/javascript', body: 'cached' }));
    const pending = fireFetch(env, { url: 'https://app.example/main.dart.js' });
    assert.ok(pending, 'listed file is handled');
    assert.equal(await (await pending).text(), 'cached');
    assert.equal(env.fetches.length, 0, 'no network request on a hit');
    console.log('PASS: a listed file is served from the build cache with no network request');
  }

  // 6. CanvasKit is cached at runtime on first fetch into this build's cache only, and the second
  //    request is served cache-first with no network. An older build's cache is never written.
  {
    const env = build();
    const old = await env.storage.open('relaydeck-shell-OLDBUILD');
    env.setRoutes({ '/canvaskit/canvaskit.wasm': response({ type: 'application/wasm', body: bodies['/canvaskit/canvaskit.wasm'] }) });
    const first = await fireFetch(env, { url: 'https://app.example/canvaskit/canvaskit.wasm' });
    assert.equal(first.ok, true);
    assert.ok((await env.storage.open(env.names.CACHE)).store.has('/canvaskit/canvaskit.wasm'));
    assert.equal(env.fetches.length, 1);
    const second = await fireFetch(env, { url: 'https://app.example/canvaskit/canvaskit.wasm' });
    assert.equal(second.ok, true);
    assert.equal(env.fetches.length, 1, 'second request is cache-first');
    assert.equal(old.store.size, 0, 'older build cache untouched');
    console.log('PASS: CanvasKit is cached at runtime into this build only, then cache-first');
  }

  // 7. Requests that must never be intercepted are left to the network.
  {
    const env = build();
    const passes = [
      { url: 'https://app.example/v1/auth/recovery.js' },
      { url: 'https://app.example/v1/hosts/h1/agents' },
      { url: 'https://app.example/ws' },
      { url: 'https://app.example/pyodide/pyodide.mjs' },
      { url: 'https://app.example/python-worker.js' },
      { url: 'https://app.example/assets/app/app.zip' },
      { url: 'https://app.example/flutter_service_worker.js' },
      { url: 'https://app.example/rd-sw-reset.html' },
      { url: 'https://app.example/main.dart.js', method: 'POST' },
      { url: 'https://other.example/main.dart.js' },
    ];
    for (const request of passes) {
      assert.equal(fireFetch(env, request), null, JSON.stringify(request));
    }
    console.log('PASS: API, auth, WebSocket, pyodide, app.zip, worker, reset page, POST and cross-origin pass through');
  }

  // 8. Navigation: a page of this build is served and the bare root is refreshed in the cache.
  {
    const env = build();
    env.setRoutes({ '/': response({ body: `<meta name="rd-build" content="${env.names.BUILD}">new` }) });
    const cache = await env.storage.open(env.names.CACHE);
    cache.store.set('/', response({ body: `<meta name="rd-build" content="${env.names.BUILD}">old` }));
    const page = await fireFetch(env, { url: 'https://app.example/', mode: 'navigate' });
    assert.equal(await page.text(), `<meta name="rd-build" content="${env.names.BUILD}">new`);
    assert.equal(await (await cache.match('/')).text(), `<meta name="rd-build" content="${env.names.BUILD}">new`);
    console.log('PASS: a page of this build is served and cached at the root');
  }

  // 9. Navigation: a deep link of this build is served but not cached under the root.
  {
    const env = build();
    env.setRoutes({ '/agents/x': response({ body: `<meta name="rd-build" content="${env.names.BUILD}">deep` }) });
    const cache = await env.storage.open(env.names.CACHE);
    cache.store.set('/', response({ body: 'shell' }));
    const page = await fireFetch(env, { url: 'https://app.example/agents/x', mode: 'navigate' });
    assert.equal(await page.text(), `<meta name="rd-build" content="${env.names.BUILD}">deep`);
    assert.equal(await (await cache.match('/')).text(), 'shell', 'root cache unchanged');
    console.log('PASS: a deep link is served and does not overwrite the cached root');
  }

  // 10. Navigation: another build's page is not served; this build's shell is.
  {
    const env = build();
    env.setRoutes({ '/': response({ body: '<meta name="rd-build" content="another">' }) });
    const cache = await env.storage.open(env.names.CACHE);
    cache.store.set('/', response({ body: `<meta name="rd-build" content="${env.names.BUILD}">shell` }));
    const page = await fireFetch(env, { url: 'https://app.example/', mode: 'navigate' });
    assert.match(await page.text(), /shell$/);
    console.log('PASS: another build page is not served; the pinned shell is');
  }

  // 11. Navigation: a network failure, and a timeout, both fall back to the pinned shell.
  {
    const env = build();
    env.setRoutes({ '/': new Error('offline') });
    const cache = await env.storage.open(env.names.CACHE);
    cache.store.set('/', response({ body: 'shell-offline' }));
    const page = await fireFetch(env, { url: 'https://app.example/', mode: 'navigate' });
    assert.equal(await page.text(), 'shell-offline');
    const slow = build();
    slow.setRoutes({ '/': 'hang' });
    slow.setTimeoutFires(true);
    const slowCache = await slow.storage.open(slow.names.CACHE);
    slowCache.store.set('/', response({ body: 'shell-timeout' }));
    const timedOut = await fireFetch(slow, { url: 'https://app.example/', mode: 'navigate' });
    assert.equal(await timedOut.text(), 'shell-timeout');
    console.log('PASS: offline and timeout fall back to the pinned shell');
  }

  // 12. Navigation with a query string (credentials possible) is never cached.
  {
    const env = build();
    env.setRoutes({ '/': response({ body: `<meta name="rd-build" content="${env.names.BUILD}">q` }) });
    const cache = await env.storage.open(env.names.CACHE);
    cache.store.set('/', response({ body: 'before' }));
    await fireFetch(env, { url: 'https://app.example/?token=secret', mode: 'navigate' });
    assert.equal(await (await cache.match('/')).text(), 'before');
    console.log('PASS: a navigation with a query string is not cached');
  }

  // 13. Activate deletes only other RelayDeck shell caches.
  {
    const env = build();
    await env.storage.open('relaydeck-shell-OLDBUILD');
    await env.storage.open(env.names.CACHE);
    await env.storage.open('other-app-cache');
    let pending = null;
    env.listeners.activate({ waitUntil(promise) { pending = promise; } });
    await pending;
    assert.deepEqual((await env.storage.keys()).sort(), [env.names.CACHE, 'other-app-cache'].sort());
    console.log('PASS: activate deletes only other relaydeck-shell caches');
  }
  // 14. Runtime hash check: the matching bytes are cached; a mismatch and an unlisted path are not.
  {
    const env = build();
    const cache = await env.storage.open(env.names.CACHE);
    env.setRoutes({
      '/canvaskit/chromium/canvaskit.wasm': response({ type: 'application/wasm', body: 'tampered bytes' }),
      '/canvaskit/unlisted.wasm': response({ type: 'application/wasm', body: 'anything' }),
    });
    const tampered = await fireFetch(env, { url: 'https://app.example/canvaskit/chromium/canvaskit.wasm' });
    assert.equal(await tampered.text(), 'tampered bytes', 'the network response is still served');
    assert.equal(cache.store.has('/canvaskit/chromium/canvaskit.wasm'), false, 'mismatch not cached');
    await fireFetch(env, { url: 'https://app.example/canvaskit/unlisted.wasm' });
    assert.equal(cache.store.has('/canvaskit/unlisted.wasm'), false, 'unlisted path not cached');
    console.log('PASS: runtime fetches with a wrong or unlisted hash are served but not cached');
  }

  // 15. Messages: the build is answered on the port; activation needs the exact build and a nonce.
  {
    const env = build();
    const nonce = '0123456789abcdef0123456789abcdef';
    const port = { messages: [], postMessage(m) { this.messages.push(m); } };
    const window = { id: 'w1' };
    env.setWindows([window]);
    const send = (data, source = window) => {
      const pending = [];
      env.listeners.message({ data, source, ports: [port], waitUntil(p) { pending.push(p); } });
      return Promise.all(pending);
    };
    await send({ type: 'RD_BUILD?' });
    assert.deepEqual(plain(port.messages.pop()), { type: 'RD_BUILD', build: env.names.BUILD });
    await send({ type: 'RD_ACTIVATE', build: 'another-build', nonce });
    assert.equal(port.messages.pop().reason, 'bad_request');
    await send({ type: 'RD_ACTIVATE', build: env.names.BUILD, nonce: 'not-a-nonce' });
    assert.equal(port.messages.pop().reason, 'bad_request');
    await send('not an object');
    assert.equal(port.messages.length, 0, 'ignored');
    assert.equal(env.skips.count, 0, 'no activation for rejected requests');
    console.log('PASS: the worker answers its build and rejects other-build or malformed activation');
  }

  // 16. Activation: one window (the requester) activates; two windows, or another window, refuse.
  {
    const nonce = 'fedcba9876543210fedcba9876543210';
    const requester = { id: 'w1' };
    const other = { id: 'w2' };
    const run = async (windows, source) => {
      const env = build();
      env.setWindows(windows);
      const port = { messages: [], postMessage(m) { this.messages.push(m); } };
      const pending = [];
      env.listeners.message({
        data: { type: 'RD_ACTIVATE', build: env.names.BUILD, nonce }, source, ports: [port],
        waitUntil(p) { pending.push(p); },
      });
      await Promise.all(pending);
      return { env, reply: port.messages[0] };
    };
    const single = await run([requester], requester);
    assert.deepEqual(plain(single.reply), { type: 'RD_ACTIVATE_ACCEPTED', nonce });
    assert.equal(single.env.skips.count, 1, 'skipWaiting only after the single-window check');
    const twice = await run([requester, other], requester);
    assert.deepEqual(plain(twice.reply), { type: 'RD_ACTIVATE_REFUSED', reason: 'other_windows', count: 2 });
    assert.equal(twice.env.skips.count, 0);
    const notMine = await run([other], requester);
    assert.equal(notMine.reply.type, 'RD_ACTIVATE_REFUSED');
    assert.equal(notMine.env.skips.count, 0);
    console.log('PASS: only the single requesting window activates; two windows are refused with a count');
  }
  // 3. A push shows only the generic server text; a bad route token is dropped, never used.
  {
    const pushOf = (env, doc) => {
      let pending = null;
      const data = doc === 'not-json' ? { json() { throw new SyntaxError('bad'); } }
        : { json() { return doc; } };
      env.listeners.push({ data, waitUntil(p) { pending = p; } });
      return pending;
    };
    const env = build();
    await pushOf(env, { b: 'An agent finished', r: 'wp_abcdefghijklmnopqrstu' });
    assert.equal(env.shown.length, 1);
    assert.equal(env.shown[0].title, 'RelayDeck');
    assert.equal(env.shown[0].options.body, 'An agent finished');
    assert.deepEqual(plain(env.shown[0].options.data), { route: 'wp_abcdefghijklmnopqrstu' });

    await pushOf(env, { b: 'x', r: '../../etc/passwd' });
    assert.equal(env.shown[1].options.body, 'x');
    assert.deepEqual(plain(env.shown[1].options.data), { route: '' }, 'bad route dropped');

    await pushOf(env, 'not-json');
    assert.equal(env.shown[2].options.body, 'An agent needs attention', 'fallback when unreadable');

    await pushOf(env, { b: 'y'.repeat(500), r: 'wp_abcdefghijklmnopqrstu' });
    assert.equal(env.shown[3].options.body, 'An agent needs attention', 'oversize body replaced');
    console.log('PASS: push shows generic text only; bad route, bad JSON and oversize bodies fall back');
  }

  // 4. A click opens the app at a fixed same-origin URL; it never leaves the origin.
  {
    const clickOf = async (env, route) => {
      let pending = null;
      env.listeners.notificationclick({
        notification: { data: { route }, close() {} },
        waitUntil(p) { pending = p; },
      });
      await pending;
    };
    const token = 'wp_abcdefghijklmnopqrstu';
    const env = build();
    await clickOf(env, token);
    assert.deepEqual(plain(env.opened), [`https://app.example/?rd_route=${token}`]);
    await clickOf(env, 'https://evil.example/');
    assert.equal(env.opened[1], 'https://app.example/', 'non-route value falls back to the root');

    const own = { url: 'https://app.example/', focused: 0, focus() { this.focused += 1; } };
    const foreign = { url: 'https://evil.example/', focused: 0, focus() { this.focused += 1; } };
    env.setWindows([foreign]);
    await clickOf(env, token);
    assert.equal(foreign.focused, 0, 'cross-origin client is never focused');
    assert.equal(env.opened.length, 3, 'opens a new same-origin window instead');

    env.setWindows([foreign, own]);
    await clickOf(env, token);
    assert.equal(own.focused, 1, 'same-origin client is focused');
    assert.equal(env.opened.length, 3, 'no new window when one is open');
    console.log('PASS: click focuses only same-origin clients and opens only fixed same-origin URLs');
  }
  console.log('ALL PASS');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
