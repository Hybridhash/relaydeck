// The notification banner in scripts/web/rd-sw-client.js, run in a VM against
// a small fake DOM, storage, service worker and push manager. Usage: node push_banner_browser.cjs <file>
// This checks the banner's decisions, not a real browser: no network, no real push service.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const KEY_PATH = '/v1/push/web/key';
const SUB_PATH = '/v1/push/web/subscription';
const CSRF = 'synthetic-csrf-token';
const DAY = 24 * 60 * 60 * 1000;

// A style object that understands the cssText assignments the script makes.
class Style {
  set cssText(text) {
    for (const part of String(text).split(';')) {
      const at = part.indexOf(':');
      if (at > 0) this[part.slice(0, at).trim()] = part.slice(at + 1).trim();
    }
  }
}

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.style = new Style();
    this.attributes = {};
    this.handlers = {};
    this.children = [];
    this._text = '';
    this.disabled = false;
    this.id = '';
    this.parent = null;
  }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join('');
  }
  set textContent(value) {
    this._text = String(value);
    if (value === '') this.children = [];
  }
  get firstChild() { return this.children[0] || null; }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  insertBefore(child, reference) {
    child.parent = this;
    const at = this.children.indexOf(reference);
    if (at < 0) this.children.unshift(child); else this.children.splice(at, 0, child);
    return child;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); }
  click() { (this.handlers.click || []).forEach((fn) => fn({})); }
  find(predicate) {
    for (const child of this.children) {
      if (predicate(child)) return child;
      const inside = child.find(predicate);
      if (inside) return inside;
    }
    return null;
  }
}

function scenario(options = {}) {
  const {
    paired = true,
    subscribed = false,
    permission = 'default',
    grant = 'granted',
    userAgent = 'Mozilla/5.0 (X11; Linux x86_64) Chrome/120',
    platform = 'Linux x86_64',
    touch = 0,
    standalone = false,
    noNotification = false,
    pushSupport = true,
    storage = new Map(),
    throwStorage = false,
    putStatus = 201,
    choice = ['blocked', 'done'],
  } = options;

  const body = new Element('body');
  const state = { paired, choice };
  const timers = [];
  const docListeners = {};
  const requests = [];
  const subscribeCalls = [];
  const listeners = {};
  const notification = {
    permission,
    prompts: 0,
    requestPermission() {
      notification.prompts += 1;
      notification.permission = grant;
      return Promise.resolve(grant);
    },
  };
  const subscription = {
    toJSON: () => ({
      endpoint: 'https://fcm.googleapis.com/fcm/send/synthetic',
      keys: { p256dh: 'synthetic-p256dh', auth: 'synthetic-auth' },
    }),
    unsubscribe: async () => true,
  };
  const registration = {
    waiting: null,
    installing: null,
    addEventListener() {},
    update: async () => {},
    pushManager: {
      subscribe: async (options) => { subscribeCalls.push(options); return subscription; },
      getSubscription: async () => (subscribed ? subscription : null),
    },
  };
  const serviceWorker = {
    controller: null,
    register: async () => registration,
    ready: Promise.resolve(registration),
    addEventListener() {},
  };

  const context = {
    console,
    Promise,
    JSON,
    Number,
    String,
    Date,
    Uint8Array,
    TextEncoder,
    atob: (text) => Buffer.from(text, 'base64').toString('binary'),
    // Short delays run at once (as the other checks expect); long ones wait to be fired.
    setTimeout: (fn, ms = 0) => { if (ms >= 10000) timers.push(fn); else fn(); return timers.length; },
    clearTimeout() {},
    crypto: require('node:crypto').webcrypto,
    fetch: async (url, init = {}) => {
      requests.push({ url, method: init.method || 'GET', headers: init.headers || {}, body: init.body });
      if (url === KEY_PATH) {
        const doc = { publicKey: 'synthetic-public-key', csrf: CSRF, subscribed, choice: state.choice };
        return state.paired
          ? { ok: true, status: 200, json: async () => doc }
          : { ok: false, status: 401, json: async () => ({}) };
      }
      if (url === SUB_PATH) return { ok: putStatus < 300, status: putStatus, json: async () => ({}) };
      return { ok: false, status: 404, json: async () => ({}) };
    },
    location: { protocol: 'https:', hostname: 'app.example' },
    document: {
      body,
      visibilityState: 'visible',
      activeElement: null,
      createElement: (tag) => new Element(tag),
      querySelector: (selector) => (selector === 'meta[name="rd-build"]'
        ? { content: '0123456789abcdef' } : null),
      getElementById: () => null,
      addEventListener: (type, fn) => { docListeners[type] = fn; },
    },
    navigator: {
      userAgent,
      platform,
      maxTouchPoints: touch,
      onLine: true,
      standalone,
      serviceWorker,
    },
  };
  const storageArea = {
    getItem(key) {
      if (throwStorage) throw new Error('storage blocked');
      return storage.has(key) ? storage.get(key) : null;
    },
    setItem(key, value) {
      if (throwStorage) throw new Error('storage blocked');
      storage.set(key, String(value));
    },
    removeItem(key) {
      if (throwStorage) throw new Error('storage blocked');
      storage.delete(key);
    },
  };
  // The page's window is the global object of the script.
  const window = context;
  window.window = window;
  window.localStorage = storageArea;
  window.sessionStorage = storageArea;
  if (!noNotification) window.Notification = notification;
  window.matchMedia = (query) => ({ matches: standalone && query.includes('standalone') });
  window.addEventListener = (type, fn) => { listeners[type] = fn; };
  if (pushSupport) window.PushManager = function PushManager() {};
  vm.createContext(window);
  vm.runInContext(source, window);

  return {
    window, body, requests, subscribeCalls, notification, storage, listeners,
    bar: () => body.find((node) => node.id === 'rd-push-bar'),
    async load() {
      listeners.load();
      await settle();
    },
    pair() { state.paired = true; },
    choose(next) { state.choice = next; },
    pendingTimers: () => timers.length,
    async fireTimers() {
      const due = timers.splice(0, timers.length);
      due.forEach((fn) => fn());
      await settle();
    },
    async foreground() {
      docListeners.visibilitychange();
      await settle();
    },
  };
}

async function settle() {
  for (let i = 0; i < 25; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

function visible(env) {
  const bar = env.bar();
  return Boolean(bar) && bar.style.display === 'flex';
}

const IOS = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148';

const tests = [];
const test = (name, run) => tests.push({ name, run });

test('an unpaired browser shows no banner at all', async () => {
  const env = scenario({ paired: false });
  await env.load();
  assert.equal(visible(env), false);
});

test('permission default and not subscribed: the "Get notifications" banner, pinned over the app', async () => {
  const env = scenario();
  await env.load();
  assert.equal(visible(env), true);
  // The app's full-screen view draws over ordinary page flow, so the bar is pinned above it.
  assert.equal(env.bar().style.position, 'fixed');
  assert.equal(env.bar().style.top, '0');
  assert.equal(env.bar().style['z-index'], '2147483645');
  assert.match(env.bar().style.padding, /safe-area-inset-top/);
  assert.match(env.bar().textContent, /Get notifications when an agent needs you/);
  assert.equal(env.notification.prompts, 0, 'no permission prompt before a tap');
});

test('a subscribed device hides the banner on load, even with permission still default', async () => {
  const env = scenario({ subscribed: true, permission: 'default' });
  await env.load();
  assert.equal(visible(env), false);
});

test('granted and subscribed: hidden', async () => {
  const env = scenario({ subscribed: true, permission: 'granted' });
  await env.load();
  assert.equal(visible(env), false);
});

test('granted but not subscribed (turned off in Settings or key rotated): "Turn on" without a prompt', async () => {
  const env = scenario({ subscribed: false, permission: 'granted' });
  await env.load();
  assert.equal(visible(env), true);
  assert.match(env.bar().textContent, /Notifications are off for this device/);
  env.bar().find((node) => node.textContent === 'Turn on').click();
  await settle();
  assert.equal(env.notification.prompts, 0, 'no prompt: permission was already granted');
  assert.equal(env.subscribeCalls.length, 1);
  assert.equal(visible(env), false, 'hidden for good once subscribed');
});

test('permission denied: a hint to the browser setting, with nothing to tap but ×', async () => {
  const env = scenario({ permission: 'denied' });
  await env.load();
  assert.equal(visible(env), true);
  assert.match(env.bar().textContent, /Notifications are blocked/);
  assert.equal(env.bar().find((node) => node.tagName === 'BUTTON' && node.textContent !== '×') ?? null, null);
  assert.equal(env.notification.prompts, 0);
});

test('denied on iOS names the system Settings path', async () => {
  const env = scenario({ permission: 'denied', userAgent: IOS, platform: 'iPhone', touch: 5, standalone: true });
  await env.load();
  assert.match(env.bar().textContent, /Settings → Notifications → RelayDeck/);
});

test('pairing inside the running app: the banner appears on the next check, without a reload', async () => {
  const env = scenario({ paired: false });
  await env.load();
  assert.equal(visible(env), false);
  assert.equal(env.pendingTimers(), 1, 'one re-check is scheduled, not a loop');
  env.pair();
  await env.fireTimers();
  assert.equal(visible(env), true);
  assert.equal(env.pendingTimers(), 1, 'paired: one slower re-check that follows the choice');
});

test('coming back to the foreground re-checks: a permission granted in Settings offers Turn on', async () => {
  const env = scenario({ permission: 'denied' });
  await env.load();
  env.notification.permission = 'granted';
  await env.foreground();
  assert.match(env.bar().textContent, /Notifications are off for this device/);
});

test('iOS outside the Home Screen app: the hint, and no button', async () => {
  const env = scenario({ userAgent: IOS, platform: 'iPhone', touch: 5, standalone: false });
  await env.load();
  assert.equal(visible(env), true);
  assert.match(env.bar().textContent, /add RelayDeck to your Home Screen \(Share → Add to Home Screen\)/);
  assert.equal(env.bar().find((node) => node.textContent === 'Get notifications'), null);
});

test('iPad Safari tab without a Notification API: the Home Screen hint still shows', async () => {
  const env = scenario({
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15',
    platform: 'MacIntel', touch: 5, standalone: false, noNotification: true,
  });
  await env.load();
  assert.equal(visible(env), true);
  assert.match(env.bar().textContent, /add RelayDeck to your Home Screen/);
});

test('iOS in the Home Screen app: the normal banner', async () => {
  const env = scenario({ userAgent: IOS, platform: 'iPhone', touch: 5, standalone: true });
  await env.load();
  assert.equal(visible(env), true);
  assert.match(env.bar().textContent, /Get notifications when an agent needs you/);
});

test('no push support and not iOS: no banner', async () => {
  const env = scenario({ pushSupport: false });
  await env.load();
  assert.equal(visible(env), false);
});

test('tapping "Get notifications" asks, subscribes with the CSRF token, then hides for good', async () => {
  const env = scenario();
  await env.load();
  env.bar().find((node) => node.textContent === 'Get notifications').click();
  await settle();
  assert.equal(env.notification.prompts, 1, 'the prompt appears only after the tap');
  assert.equal(env.subscribeCalls.length, 1);
  const put = env.requests.find((r) => r.method === 'PUT');
  assert.equal(put.url, SUB_PATH);
  assert.equal(put.headers['X-RelayDeck-CSRF'], CSRF);
  assert.deepEqual(JSON.parse(put.body), {
    endpoint: 'https://fcm.googleapis.com/fcm/send/synthetic',
    keys: { p256dh: 'synthetic-p256dh', auth: 'synthetic-auth' },
  }, 'only the endpoint and the two keys leave the browser');
  assert.equal(visible(env), false);
});

test('a refused subscription shows a short error, and nothing else', async () => {
  const env = scenario({ putStatus: 400 });
  await env.load();
  env.bar().find((node) => node.textContent === 'Get notifications').click();
  await settle();
  assert.match(env.bar().textContent, /Could not turn on notifications/);
});

test('"×" hides the banner for this run only: it stays hidden on return, and is back on reopen', async () => {
  const storage = new Map();
  const first = scenario({ storage });
  await first.load();
  first.bar().find((node) => node.attributes['aria-label'] === 'Hide until RelayDeck is opened again').click();
  assert.equal(visible(first), false);
  await first.foreground();
  assert.equal(visible(first), false, 'coming back from another app does not bring it back');
  assert.ok(![...storage.keys()].some((key) => key.startsWith('rd-push')), 'nothing is remembered across launches');
  const reopened = scenario({ storage });
  await reopened.load();
  assert.equal(visible(reopened), true, 'reopening RelayDeck offers it again');
});

test('a 30-day dismissal left by an older build is cleared, so the banner shows', async () => {
  const storage = new Map([['rd-push-dismissed-until', String(Date.now() + 29 * DAY)]]);
  const env = scenario({ storage });
  await env.load();
  assert.equal(visible(env), true);
  assert.equal(storage.has('rd-push-dismissed-until'), false);
});

test('blocked storage: the banner still shows, and "×" hides it without throwing', async () => {
  const env = scenario({ throwStorage: true });
  await env.load();
  assert.equal(visible(env), true);
  env.bar().find((node) => node.attributes['aria-label'] === 'Hide until RelayDeck is opened again').click();
  assert.equal(visible(env), false);
});

test('a closed bar comes back when the choice for this device changes in Settings', async () => {
  const env = scenario();
  await env.load();
  env.bar().find((node) => node.attributes['aria-label'] === 'Hide until RelayDeck is opened again').click();
  await env.fireTimers();
  assert.equal(visible(env), false, 'the same choice keeps it closed');
  env.choose(['blocked']);
  await env.fireTimers();
  assert.equal(visible(env), true, 'a new choice offers it again');
  assert.match(env.bar().textContent, /Get notifications/);
});

test('a choice of none shows no bar; choosing something shows it', async () => {
  const env = scenario({ choice: [] });
  await env.load();
  assert.equal(visible(env), false);
  env.choose(['done']);
  await env.fireTimers();
  assert.equal(visible(env), true);
});

test('a subscribed device schedules no re-checks', async () => {
  const env = scenario({ subscribed: true, permission: 'granted' });
  await env.load();
  assert.equal(env.pendingTimers(), 0);
});

test('a re-check while RelayDeck is in the background asks nothing', async () => {
  const env = scenario();
  await env.load();
  const before = env.requests.length;
  env.window.document.visibilityState = 'hidden';
  await env.fireTimers();
  assert.equal(env.requests.length, before);
});

test('the Settings DELETE is not part of the banner: no Turn off control here', async () => {
  const env = scenario({ subscribed: true, permission: 'granted' });
  await env.load();
  assert.equal(visible(env), false);
  assert.equal(env.bar()?.find((node) => node.textContent === 'Turn off') ?? null, null);
  assert.equal(env.requests.some((r) => r.method === 'DELETE'), false);
});

(async () => {
  let failed = 0;
  for (const { name, run } of tests) {
    try {
      await run();
      console.log(`PASS: ${name}`);
    } catch (error) {
      failed += 1;
      console.error(`FAIL: ${name}\n${error.stack || error}`);
    }
  }
  if (failed) {
    console.error(`${failed} of ${tests.length} failed`);
    process.exitCode = 1;
  } else {
    console.log('ALL PASS');
  }
})();
