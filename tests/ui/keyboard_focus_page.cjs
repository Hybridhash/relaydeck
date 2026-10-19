// The page guard (scripts/web/rd-keyboard-focus.js) against a fake DOM. Usage:
//   node keyboard_focus_page.cjs <rd-keyboard-focus.js>
// Checks logic only; no browser. No network.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(process.argv[2], 'utf8');

class Element {
  constructor(tag, parent) {
    this.tagName = tag.toUpperCase();
    this.nodeType = 1;
    this.parentNode = parent || null;
  }
}

function makeEnv({ vendor = 'Apple Computer, Inc.', touchPoints = 5 } = {}) {
  const body = new Element('body');
  const view = new Element('flutter-view', body);
  const glass = new Element('flt-glass-pane', view);
  const editingHost = new Element('flt-text-editing-host', view);
  const textarea = new Element('textarea', editingHost);
  const semanticsHost = new Element('flt-semantics-host', view);
  const semanticsButton = new Element('flt-semantics', semanticsHost);
  const outside = new Element('div', body);
  const listeners = {};
  const doc = {
    body,
    activeElement: body,
    addEventListener(type, fn, options) {
      (listeners[type] = listeners[type] || []).push({ fn, options });
    },
  };
  const context = vm.createContext({
    document: doc,
    navigator: { vendor, maxTouchPoints: touchPoints },
    console,
  });
  vm.runInContext(source, context);
  function tap(target, { cancelable = true } = {}) {
    let prevented = false;
    const event = {
      type: 'touchend',
      target,
      cancelable,
      preventDefault() { prevented = true; },
    };
    for (const { fn } of listeners.touchend || []) fn(event);
    return prevented;
  }
  return { doc, listeners, tap, view, glass, textarea, semanticsButton, outside, body };
}

{
  const env = makeEnv();
  const [{ options }] = env.listeners.touchend;
  assert.equal(options.capture, true);
  assert.equal(options.passive, false);
  // No Flutter input focused: taps are left alone.
  assert.equal(env.tap(env.glass), false);
  // Input focused: a tap on Flutter-drawn content keeps the default from moving focus.
  env.doc.activeElement = env.textarea;
  assert.equal(env.tap(env.glass), true);
  assert.equal(env.tap(env.view), true);
  // The input itself, the semantics tree and anything outside the view are untouched.
  assert.equal(env.tap(env.textarea), false);
  assert.equal(env.tap(env.semanticsButton), false);
  assert.equal(env.tap(env.outside), false);
  assert.equal(env.tap(env.body), false);
  // A touch the browser already owns (not cancelable) is not fought over.
  assert.equal(env.tap(env.glass, { cancelable: false }), false);
  // Focus on a non-Flutter element does not arm the guard.
  env.doc.activeElement = env.outside;
  assert.equal(env.tap(env.glass), false);
}

{
  // Other engines and non-touch Apple devices install nothing.
  const chrome = makeEnv({ vendor: 'Google Inc.' });
  assert.equal(chrome.listeners.touchend, undefined);
  const mac = makeEnv({ touchPoints: 0 });
  assert.equal(mac.listeners.touchend, undefined);
}

console.log('ALL PASS');
