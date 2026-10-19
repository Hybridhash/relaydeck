// RelayDeck keyboard focus guard. Inlined into the served index by
// scripts/web_service_worker.py. Page side only, iOS WebKit only.
//
// A tap on Flutter-drawn content while a Flutter text input holds DOM focus
// must not move that focus. WebKit otherwise blurs the input as the tap's
// default action, which hides the keyboard; the engine then either refocuses
// the input (a one to two second dip) or, when focus went nowhere, reports the
// view unfocused and the framework drops the field's focus for good. Flutter
// only sees pointer events, which this never touches, so its own focus rules
// still decide: a field without a tap-outside handler still closes.
(function () {
  'use strict';
  var apple = typeof navigator !== 'undefined' && navigator.vendor &&
    navigator.vendor.indexOf('Apple') === 0;
  var touch = typeof navigator !== 'undefined' && navigator.maxTouchPoints > 1;
  if (!apple || !touch) return;

  var VIEW = 'FLUTTER-VIEW';
  var EDITING = 'FLT-TEXT-EDITING-HOST';
  var SEMANTICS = 'FLT-SEMANTICS-HOST';

  function within(node, tag) {
    for (var el = node; el && el.nodeType === 1; el = el.parentNode) {
      if (el.tagName === tag) return true;
    }
    return false;
  }

  function flutterInput(el) {
    return !!el && el.nodeType === 1 &&
      (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') && within(el, EDITING);
  }

  document.addEventListener('touchend', function (event) {
    if (!flutterInput(document.activeElement)) return;
    var target = event.target;
    if (!within(target, VIEW) || within(target, EDITING) || within(target, SEMANTICS)) return;
    if (event.cancelable) event.preventDefault();
  }, { capture: true, passive: false });
})();
