// The page script (scripts/web/rd-sw-client.js) against a fake DOM, a fake
// service-worker container, fake workers and a controllable clock. Usage:
//   node update_notice_page.cjs <rd-sw-client.js>
// Checks logic only; no browser. No network.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const clientSource = fs.readFileSync(process.argv[2], 'utf8');
const PAGE_BUILD = 'aaaaaaaaaaaaaaaa';
const NEW_BUILD = 'bbbbbbbbbbbbbbbb';
const NEWER_BUILD = 'cccccccccccccccc';

function makeEnv({ storage = { session: new Map(), local: new Map() }, fetchImpl = null } = {}) {
  let now = 1700000000000;
  const timers = [];
  let timerId = 0;
  class FakeDate extends Date { static now() { return now; } }

  class Element {
    constructor(tag) {
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.attributes = {};
      this.style = { cssText: '', display: '' };
      this.listeners = {};
      this.disabled = false;
      this.textContent = '';
      this.isContentEditable = false;
      this.id = '';
    }
    appendChild(child) { this.children.push(child); return child; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    click() { (this.listeners.click || []).forEach((fn) => fn({ type: 'click' })); }
  }
  const body = new Element('body');
  const meta = new Element('meta');
  meta.attributes.name = 'rd-build';
  meta.content = PAGE_BUILD;
  const docListeners = {};
  const winListeners = {};
  const doc = {
    body,
    visibilityState: 'visible',
    activeElement: null,
    createElement: (tag) => new Element(tag),
    getElementById(id) {
      const stack = [...body.children];
      while (stack.length) {
        const node = stack.shift();
        if (node.id === id) return node;
        stack.push(...node.children);
      }
      return null;
    },
    querySelector: (selector) => (selector === 'meta[name="rd-build"]' ? meta : null),
    addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
    fire(type) { (docListeners[type] || []).forEach((fn) => fn({ type })); },
  };

  function storageOf() {
    const make = (map) => ({
      getItem: (k) => (map.has(k) ? map.get(k) : null),
      setItem: (k, v) => map.set(k, String(v)),
      removeItem: (k) => map.delete(k),
    });
    return { session: make(storage.session), local: make(storage.local) };
  }
  const stores = storageOf();
  const reloads = { count: 0 };
  const location = { protocol: 'https:', hostname: 'app.example', reload: () => { reloads.count += 1; } };

  // Fake service-worker container.
  const swListeners = {};
  const sw = {
    controller: null,
    registration: null,
    register: (url, options) => { sw.registerCalls = (sw.registerCalls || []).concat([[url, options]]); return Promise.resolve(sw.registration); },
    addEventListener(type, fn) { (swListeners[type] = swListeners[type] || []).push(fn); },
    removeEventListener(type, fn) { swListeners[type] = (swListeners[type] || []).filter((f) => f !== fn); },
    fire(type) { (swListeners[type] || []).slice().forEach((fn) => fn({ type })); },
  };

  class MessageChannel {
    constructor() {
      this.port1 = { onmessage: null };
      this.port2 = { postMessage: (data) => { if (this.port1.onmessage) this.port1.onmessage({ data }); } };
    }
  }
  const windowObj = {
    addEventListener(type, fn) { (winListeners[type] = winListeners[type] || []).push(fn); },
    fire(type) { (winListeners[type] || []).forEach((fn) => fn({ type })); },
    localStorage: stores.local,
    sessionStorage: stores.session,
    setTimeout: (fn, ms) => { const id = ++timerId; timers.push({ id, fn, at: now + (ms || 0) }); return id; },
    clearTimeout: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
    location,
    MutationObserver: class { observe() {} },
  };
  const navigator = { onLine: true, serviceWorker: sw };
  const context = {
    window: windowObj, document: doc, navigator, location, console,
    setTimeout: windowObj.setTimeout, clearTimeout: windowObj.clearTimeout,
    MessageChannel, MutationObserver: windowObj.MutationObserver, crypto: webcrypto, Date: FakeDate,
    Uint8Array, Promise, Math, String, Number,
  };
  if (fetchImpl) {
    // Web push is supported and undecided, so the key reply decides what the bar shows.
    context.fetch = fetchImpl;
    context.Notification = { permission: 'default' };
    windowObj.Notification = context.Notification;
    windowObj.PushManager = function PushManager() {};
  }
  vm.createContext(context);
  vm.runInContext(clientSource, context);

  // Advance the clock and run every timer that is due, in order.
  function advance(ms) {
    now += ms;
    let ran = true;
    while (ran) {
      ran = false;
      timers.sort((a, b) => a.at - b.at);
      const due = timers.find((t) => t.at <= now);
      if (due) {
        timers.splice(timers.indexOf(due), 1);
        due.fn();
        ran = true;
      }
    }
  }
  async function tick() { for (let i = 0; i < 30; i += 1) await Promise.resolve(); }

  return { doc, body, sw, stores, reloads, windowObj, navigator, advance, tick, fire: (t) => windowObj.fire(t), docFire: (t) => doc.fire(t), advanceNow: () => now, timers };
}

// A fake waiting/installing worker. `handler(worker, message, port)` answers activation messages.
function makeWorker(build, handler) {
  const listeners = {};
  const worker = {
    build,
    state: 'installed',
    messages: [],
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter((f) => f !== fn); },
    setState(state) { worker.state = state; (listeners.statechange || []).slice().forEach((fn) => fn({ type: 'statechange' })); },
    postMessage(message, transfer) {
      worker.messages.push(message);
      const port = transfer && transfer[0];
      if (message.type === 'RD_BUILD?') { port.postMessage({ type: 'RD_BUILD', build: worker.build }); return; }
      if (handler) handler(worker, message, port);
    },
  };
  return worker;
}

function makeRegistration({ waiting = null, active = null } = {}) {
  const listeners = {};
  const reg = {
    waiting, installing: null, active, updates: 0,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    fire(type) { (listeners[type] || []).forEach((fn) => fn({ type })); },
    update() { reg.updates += 1; return Promise.resolve(); },
  };
  return reg;
}

async function boot(env, reg, controller) {
  env.sw.registration = reg;
  env.sw.controller = controller;
  env.windowObj.fire('load');
  await env.tick();
  return reg;
}

function barOf(env) { return env.doc.getElementById('rd-update-bar'); }
function buttons(env) {
  const bar = barOf(env);
  return bar ? bar.children.filter((c) => c.tagName === 'BUTTON') : [];
}
function note(env) { const n = env.doc.getElementById('rd-update-note'); return n ? n.textContent : ''; }
function shown(env) { const bar = barOf(env); return !!bar && bar.style.display === 'flex'; }

async function scenarios() {
  // 1. First install (no controlling worker): no bar, even with a waiting worker.
  {
    const env = makeEnv();
    const waiting = makeWorker(NEW_BUILD);
    await boot(env, makeRegistration({ waiting }), null);
    assert.equal(shown(env), false);
    assert.equal(barOf(env), null);
    console.log('PASS: first install shows no update notice');
  }

  // 2. Update installed while controlled: the bar appears with Reload and Later.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    await boot(env, makeRegistration({ waiting: makeWorker(NEW_BUILD), active: controller }), controller);
    assert.equal(shown(env), true);
    assert.deepEqual(buttons(env).map((b) => b.textContent), ['Reload', 'Later']);
    assert.equal(barOf(env).attributes.role, 'status');
    assert.equal(barOf(env).attributes['aria-live'], 'polite');
    assert.match(barOf(env).style.cssText, /env\(safe-area-inset-bottom\)/);
    console.log('PASS: an update installed while controlled shows the bar');
  }

  // 3. Later suppresses only that build; a newer build notifies again.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    const reg = makeRegistration({ waiting: makeWorker(NEW_BUILD), active: controller });
    await boot(env, reg, controller);
    buttons(env)[1].click();
    assert.equal(shown(env), false);
    assert.equal(env.stores.session.getItem('rd-update-dismissed'), NEW_BUILD);
    // Same session storage, same waiting build: no notice.
    const sameStore = makeEnv({ storage: { session: new Map([['rd-update-dismissed', NEW_BUILD]]), local: new Map() } });
    await boot(sameStore, makeRegistration({ waiting: makeWorker(NEW_BUILD), active: controller }), controller);
    assert.equal(shown(sameStore), false);
    const newer = makeEnv({ storage: { session: new Map([['rd-update-dismissed', NEW_BUILD]]), local: new Map() } });
    await boot(newer, makeRegistration({ waiting: makeWorker(NEWER_BUILD), active: controller }), controller);
    assert.equal(shown(newer), true);
    console.log('PASS: Later suppresses only that build');
  }

  // 4. Single window, matching consent: one RD_ACTIVATE with the nonce, then exactly one reload
  //    after the waiting worker is activated; the watchdog cooldown is stamped first.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    const waiting = makeWorker(NEW_BUILD, (worker, message, port) => {
      if (message.type === 'RD_ACTIVATE') port.postMessage({ type: 'RD_ACTIVATE_ACCEPTED', nonce: message.nonce });
    });
    await boot(env, makeRegistration({ waiting, active: controller }), controller);
    buttons(env)[0].click();
    env.advance(1600);
    await env.tick();
    const activate = waiting.messages.find((m) => m.type === 'RD_ACTIVATE');
    assert.ok(activate, 'RD_ACTIVATE posted');
    assert.equal(activate.build, NEW_BUILD);
    assert.match(activate.nonce, /^[0-9a-f]{32}$/);
    assert.equal(env.reloads.count, 0, 'no reload before activation');
    waiting.setState('activated');
    await env.tick();
    assert.equal(env.reloads.count, 1, 'exactly one reload');
    assert.equal(env.stores.session.getItem('_rd_watchdog_reload'), String(env.advanceNow()));
    assert.equal(waiting.messages.filter((m) => m.type === 'RD_ACTIVATE').length, 1);
    console.log('PASS: single window consent: one activation message and one reload, cooldown stamped');
  }

  // 5. Repeated taps: one activation message.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    const waiting = makeWorker(NEW_BUILD, () => {});
    await boot(env, makeRegistration({ waiting, active: controller }), controller);
    buttons(env)[0].click();
    buttons(env)[0].click();
    env.advance(1600);
    await env.tick();
    assert.equal(waiting.messages.filter((m) => m.type === 'RD_ACTIVATE').length, 1);
    console.log('PASS: repeated taps send one activation');
  }

  // 6. Two windows: the worker refuses; the bar says to close the other windows; no reload.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    const waiting = makeWorker(NEW_BUILD, (worker, message, port) => {
      if (message.type === 'RD_ACTIVATE') port.postMessage({ type: 'RD_ACTIVATE_REFUSED', reason: 'other_windows', count: 2 });
    });
    await boot(env, makeRegistration({ waiting, active: controller }), controller);
    buttons(env)[0].click();
    env.advance(1600);
    await env.tick();
    assert.equal(note(env), 'Close other RelayDeck windows to update');
    assert.equal(buttons(env)[0].disabled, false);
    assert.equal(env.reloads.count, 0);
    console.log('PASS: two windows refused with a message and no reload');
  }

  // 7. controllerchange without consent does nothing.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    await boot(env, makeRegistration({ waiting: makeWorker(NEW_BUILD), active: controller }), controller);
    env.sw.fire('controllerchange');
    env.advance(20000);
    await env.tick();
    assert.equal(env.reloads.count, 0);
    console.log('PASS: controllerchange without consent does not reload');
  }

  // 8. Waiting worker gone before the tap: a clear message, no activation.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    const reg = makeRegistration({ waiting: makeWorker(NEW_BUILD), active: controller });
    await boot(env, reg, controller);
    reg.waiting = null;
    buttons(env)[0].click();
    env.advance(1600);
    await env.tick();
    assert.equal(note(env), 'Update is no longer available. Tap Reload to check again.');
    assert.equal(env.reloads.count, 0);
    console.log('PASS: a waiting worker that is gone is reported, not activated');
  }

  // 9. The waiting worker becomes redundant (superseded) after the message: reported, no reload.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    const waiting = makeWorker(NEW_BUILD, (worker, message, port) => {
      if (message.type === 'RD_ACTIVATE') { port.postMessage({ type: 'RD_ACTIVATE_ACCEPTED', nonce: message.nonce }); worker.setState('redundant'); }
    });
    await boot(env, makeRegistration({ waiting, active: controller }), controller);
    buttons(env)[0].click();
    env.advance(1600);
    await env.tick();
    assert.equal(note(env), 'Update was replaced. Tap Reload to check again.');
    assert.equal(env.reloads.count, 0);
    console.log('PASS: a redundant or superseded worker is reported, no reload');
  }

  // 10. Activation timeout: a clear error after 10 s, no blind retry or reload.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    const waiting = makeWorker(NEW_BUILD, (worker, message, port) => {
      if (message.type === 'RD_ACTIVATE') port.postMessage({ type: 'RD_ACTIVATE_ACCEPTED', nonce: message.nonce });
    });
    await boot(env, makeRegistration({ waiting, active: controller }), controller);
    buttons(env)[0].click();
    env.advance(1600);
    await env.tick();
    env.advance(10001);
    await env.tick();
    assert.equal(note(env), 'Update did not finish. Tap Reload to try again.');
    assert.equal(env.reloads.count, 0);
    assert.equal(waiting.messages.filter((m) => m.type === 'RD_ACTIVATE').length, 1, 'no blind retry');
    console.log('PASS: activation timeout shows an error, no blind retry or reload');
  }

  // 11. Input quiescence: a recent keystroke delays activation until 1.5 s of quiet.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    const waiting = makeWorker(NEW_BUILD, () => {});
    await boot(env, makeRegistration({ waiting, active: controller }), controller);
    env.doc.fire('input');
    buttons(env)[0].click();
    env.advance(1400);
    await env.tick();
    assert.equal(waiting.messages.filter((m) => m.type === 'RD_ACTIVATE').length, 0, 'still quiet-waiting');
    env.advance(200);
    await env.tick();
    assert.equal(waiting.messages.filter((m) => m.type === 'RD_ACTIVATE').length, 1);
    console.log('PASS: recent input delays activation until the field is quiet');
  }

  // 12. Foreground checks: at most one update() per 60 minutes, visibility-driven, no timers.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    const reg = makeRegistration({ active: controller });
    await boot(env, reg, controller);
    env.doc.fire('visibilitychange');
    await env.tick();
    env.doc.fire('visibilitychange');
    await env.tick();
    assert.equal(reg.updates, 1, 'throttled');
    env.advance(60 * 60 * 1000 + 1);
    env.doc.fire('visibilitychange');
    await env.tick();
    assert.equal(reg.updates, 2);
    console.log('PASS: foreground update checks are throttled to once per 60 minutes');
  }

  // 13. A recovery dialog or reconnect banner hides the bar; it returns when they are gone.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    await boot(env, makeRegistration({ waiting: makeWorker(NEW_BUILD), active: controller }), controller);
    assert.equal(shown(env), true);
    const recovery = env.doc.createElement('dialog');
    recovery.id = 'relaydeck-session-recovery';
    recovery.open = true;
    env.body.appendChild(recovery);
    env.docFire('focusin');
    assert.equal(shown(env), false);
    recovery.open = false;
    env.docFire('focusin');
    assert.equal(shown(env), true);
    const banner = env.doc.createElement('div');
    banner.id = 'relaydeck-reconnect-banner';
    env.body.appendChild(banner);
    env.docFire('focusin');
    assert.equal(shown(env), false);
    console.log('PASS: recovery dialog and reconnect banner hide the update bar');
  }

  // 14. A malformed build reply never shows the bar.
  {
    const env = makeEnv();
    const controller = makeWorker(PAGE_BUILD);
    const bad = makeWorker('not-a-build');
    bad.postMessage = (message, transfer) => transfer[0].postMessage({ type: 'RD_BUILD', build: 'not-a-build' });
    await boot(env, makeRegistration({ waiting: bad, active: controller }), controller);
    assert.equal(shown(env), false);
    console.log('PASS: a malformed build reply does not show the update');
  }

  // 15. The web-push check backs off while unpaired and resets on foreground or online.
  {
    let calls = 0;
    const env = makeEnv({ fetchImpl: () => { calls += 1; return Promise.resolve({ ok: false }); } });
    const controller = makeWorker(PAGE_BUILD);
    await boot(env, makeRegistration({ active: controller }), controller);
    assert.equal(calls, 1);
    for (const [wait, want] of [[60000, 2], [120000, 3], [300000, 4], [300000, 5]]) {
      env.advance(wait - 1);
      await env.tick();
      assert.equal(calls, want - 1, `no check before ${wait} ms`);
      env.advance(1);
      await env.tick();
      assert.equal(calls, want, `check after ${wait} ms`);
    }
    env.docFire('visibilitychange');
    await env.tick();
    assert.equal(calls, 6, 'a foreground checks at once');
    env.advance(60000);
    await env.tick();
    assert.equal(calls, 7, 'and the wait starts again at 60 s');
    env.advance(120000);
    await env.tick();
    assert.equal(calls, 8);
    env.fire('online');
    await env.tick();
    assert.equal(calls, 9, 'coming online checks at once');
    env.advance(60000);
    await env.tick();
    assert.equal(calls, 10, 'and also restarts the wait at 60 s');
    console.log('PASS: the unpaired push check backs off 60 s, 2 min, 5 min and resets');
  }

  // 16. The 15 s push check runs only while the bar is open.
  {
    let calls = 0;
    const answer = { publicKey: 'k', csrf: 'c', subscribed: false, choice: ['blocked'] };
    const env = makeEnv({ fetchImpl: () => { calls += 1; return Promise.resolve({ ok: true, json: () => Promise.resolve(answer) }); } });
    const controller = makeWorker(PAGE_BUILD);
    await boot(env, makeRegistration({ active: controller }), controller);
    const pushBar = () => env.doc.getElementById('rd-push-bar');
    assert.equal(pushBar().style.display, 'flex');
    env.advance(15000);
    await env.tick();
    assert.equal(calls, 2, 'checks every 15 s while the bar is open');
    pushBar().children.find((c) => c.textContent === '×').click();
    assert.equal(pushBar().style.display, 'none');
    env.advance(15000);
    await env.tick();
    assert.equal(calls, 3, 'the check already scheduled still runs once');
    env.advance(10 * 60 * 1000);
    await env.tick();
    assert.equal(calls, 3, 'then no timed check while closed');
    env.docFire('visibilitychange');
    await env.tick();
    assert.equal(calls, 4, 'a foreground checks as before');
    assert.equal(pushBar().style.display, 'none');
    answer.choice = ['blocked', 'done'];
    env.docFire('visibilitychange');
    await env.tick();
    assert.equal(pushBar().style.display, 'flex', 'a new choice offers the bar again');
    console.log('PASS: the 15 s push check stops once the bar is closed');
  }

  console.log('ALL PASS');
}

scenarios().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
