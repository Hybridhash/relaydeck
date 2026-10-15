// Behavioral checks for the shipped script in a Node DOM/WS test double.
// This does not constitute a rendered browser or terminal acceptance check.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync(process.argv[2], 'utf8');

function fixture(storageFails = false, responseStatus = 204, pathname = '/agents/a') {
  const elements = [];
  const removals = [];
  const storage = new Map([['_flet_session_id', 'old'], ['other', 'keep']]);
  const timers = [];
  const winEvents = {};
  const docEvents = {};
  const navigator = {onLine: true};
  let reloads = 0;
  class Element {
    constructor(tag) {
      this.tag = tag;
      this.children = [];
      this.listeners = {};
      this.attributes = {};
      this.style = {};
      elements.push(this);
    }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.children.push(child); }
    setAttribute(key, value) { this.attributes[key] = value; }
    addEventListener(event, listener) { this.listeners[event] = listener; }
    remove() { this.removed = true; }
    showModal() { this.modal = true; }
    focus() { this.focused = true; }
  }
  class NativeWebSocket {
    constructor(url, protocols) {
      this.url = url;
      this.protocols = protocols;
      this.listeners = {};
    }
    addEventListener(event, listener) { this.listeners[event] = listener; }
    closeEvent(code, reason) { this.listeners.close?.({code, reason}); }
  }
  NativeWebSocket.OPEN = 1;
  const window = {
    WebSocket: NativeWebSocket,
    location: {
      pathname,
      host: 'app.example', protocol: 'https:', href: 'https://app.example/agents/a',
      reload: () => ++reloads,
    },
    sessionStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, String(value)); },
      removeItem(key) {
        if (storageFails) throw Error('disabled');
        removals.push(key);
        storage.delete(key);
      },
    },
    setTimeout(fn, ms) { timers.push({fn, ms, active: true}); return timers.length; },
    clearTimeout(id) { if (timers[id - 1]) timers[id - 1].active = false; },
    addEventListener(event, listener) { winEvents[event] = listener; },
  };
  const document = {
    body: new Element('body'),
    readyState: 'complete',
    visibilityState: 'visible',
    createElement: (tag) => new Element(tag),
    addEventListener(event, listener) { docEvents[event] = listener; },
  };
  const fetch = async () => ({status:responseStatus});
  vm.runInNewContext(script, {window, document, navigator, URL, Reflect, Proxy, fetch});
  // Runs every timer still pending, as the 15 s watchdog would after its delay.
  const fire = () => { for (const t of timers) if (t.active) { t.active = false; t.fn(); } };
  return {
    window, document, navigator, elements, removals, storage, timers, winEvents, docEvents,
    reloads: () => reloads, NativeWebSocket, fire,
  };
}

const bannerOf = (f) => f.elements.find((e) => e.id === 'relaydeck-reconnect-banner' && !e.removed);

async function main() {
const normal = fixture();
const ws = new normal.window.WebSocket('wss://app.example/ws', ['protocol']);
assert(ws instanceof normal.NativeWebSocket);
assert.equal(normal.window.WebSocket.OPEN, 1);
assert.deepEqual(ws.protocols, ['protocol']);
ws.closeEvent(1006, '');
ws.closeEvent(1008, 'logout');
new normal.window.WebSocket('wss://app.example/v1/terminal/l').closeEvent(
  1008, 'session_recovery_required');
new normal.window.WebSocket('wss://other.example/ws').closeEvent(
  1008, 'session_recovery_required');
new normal.window.WebSocket('ws://app.example/ws').closeEvent(
  1008, 'session_recovery_required');
assert.equal(normal.elements.filter((e) => e.tag === 'dialog').length, 0);
assert.deepEqual(normal.removals, []);

ws.closeEvent(1008, 'session_recovery_required');
ws.closeEvent(1008, 'session_recovery_required');
const dialog = normal.elements.find((e) => e.tag === 'dialog');
assert.equal(normal.elements.filter((e) => e.tag === 'dialog').length, 1);
assert.equal(dialog.modal, true);
assert.equal(dialog.attributes['aria-labelledby'], 'relaydeck-recovery-title');
assert.deepEqual(normal.removals, []);
assert.equal(normal.reloads(), 0);
const button = normal.elements.find((e) => e.tag === 'button');
assert.equal(button.focused, true);
await button.listeners.click();
assert.deepEqual(normal.removals, ['_flet_session_id']);
assert.equal(normal.reloads(), 1);
assert.equal(normal.storage.get('other'), 'keep');

const denied = fixture(true);
new denied.window.WebSocket('wss://app.example/ws').closeEvent(
  1008, 'session_recovery_required');
await denied.elements.find((e) => e.tag === 'button').listeners.click();
assert.equal(denied.reloads(), 0);
assert.deepEqual(denied.removals, []);
const alert = denied.elements.find((e) => e.attributes.role === 'alert');
assert.match(alert.textContent, /Open this site in a new tab/);
const unavailable = fixture(false, 503, '/v1/auth/session/recovery');
const retry = unavailable.elements.find((e) => e.tag === 'button');
await retry.listeners.click();
assert.equal(unavailable.reloads(), 0);
assert.deepEqual(unavailable.removals, []);
assert.equal(retry.disabled, false);
assert.match(unavailable.elements.find((e) => e.attributes.role === 'alert').textContent, /unavailable/);
const revoked = fixture(false, 401, '/v1/auth/session/recovery');
assert.equal(revoked.reloads(), 0);
await revoked.elements.find((e) => e.tag === 'button').listeners.click();
assert.deepEqual(revoked.removals, ['_flet_session_id']);
assert.equal(revoked.reloads(), 1);
console.log('PASS: deliberate checked recovery, tab isolation, revocation and retry/storage failure');

// A first load shows no banner and arms no watchdog, even when the
// socket drops or the page returns before any connection has been made.
const first = fixture();
new first.window.WebSocket('wss://app.example/ws').closeEvent(1006, '');
first.docEvents.visibilitychange();
first.winEvents.pageshow({persisted: false});
assert.equal(bannerOf(first), undefined);
assert.equal(first.timers.filter((t) => t.active).length, 0);
console.log('PASS: first load shows no reconnect banner');

// A tracked /ws close after a received message shows "Reconnecting…"; the next message hides it.
const live = fixture();
const link = new live.window.WebSocket('wss://app.example/ws');
link.listeners.message({});
assert.equal(bannerOf(live), undefined);
link.closeEvent(1006, '');
const shown = bannerOf(live);
assert.equal(shown.textContent, 'Reconnecting…');
assert.equal(shown.attributes.role, 'status');
assert.equal(shown.attributes['aria-live'], 'polite');
assert.equal(live.timers.filter((t) => t.active).length, 1);
const next = new live.window.WebSocket('wss://app.example/ws');
next.listeners.message({});
assert.equal(bannerOf(live), undefined);
assert.equal(shown.removed, true);
assert.equal(live.timers.filter((t) => t.active).length, 0);
console.log('PASS: close shows the reconnect banner and a received message hides it');

// The 15 s watchdog upgrades the same banner. A recent reload in this tab blocks the reload,
// so the long-wait text with a Reload button appears; offline wins over both.
const stuck = fixture();
const dropped = new stuck.window.WebSocket('wss://app.example/ws');
dropped.listeners.message({});
dropped.closeEvent(1006, '');
stuck.storage.set('_rd_watchdog_reload', String(Date.now()));
stuck.fire();
const upgraded = bannerOf(stuck);
assert.equal(upgraded.textContent, 'Reconnecting is taking longer than expected');
assert.equal(upgraded.children.find((c) => c.tag === 'button').textContent, 'Reload');
assert.equal(stuck.reloads(), 0);

const offline = fixture();
const lost = new offline.window.WebSocket('wss://app.example/ws');
lost.listeners.message({});
offline.navigator.onLine = false;
lost.closeEvent(1006, '');
offline.fire();
assert.equal(bannerOf(offline).textContent, 'You are offline - waiting for the network');
assert.equal(offline.reloads(), 0);
console.log('PASS: watchdog upgrades the banner to its long-wait and offline texts');
}
main().catch((error) => {console.error(error); process.exitCode = 1;});
