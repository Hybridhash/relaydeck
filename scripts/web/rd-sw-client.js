// RelayDeck update notice. Inlined into the served index by
// scripts/web_service_worker.py. Page side only: it reloads only after the user asked, for the
// build they consented to, and never on an unrelated controllerchange.
(function () {
  'use strict';
  if (!('serviceWorker' in navigator)) return;
  if (!(location.protocol === 'https:' || location.hostname === 'localhost')) return;

  var meta = document.querySelector('meta[name="rd-build"]');
  var PAGE_BUILD = (meta && meta.content) || '';
  var HEX16 = /^[0-9a-f]{16}$/;
  var DISMISS_KEY = 'rd-update-dismissed';   // sessionStorage: the build the user chose Later for
  var CHECK_KEY = 'rd-update-checked-at';    // localStorage: last foreground update check (ms)
  var CHECK_GAP_MS = 60 * 60 * 1000;
  var QUIET_MS = 1500;
  var QUIET_CAP_MS = 10000;
  var ACTIVATE_TIMEOUT_MS = 10000;
  var REPLY_TIMEOUT_MS = 5000;
  var RELOAD_KEY = '_rd_watchdog_reload';    // web_recovery.py reload cooldown: stamped, never cleared

  var registration = null;
  var offered = null;                        // {build, worker} for the notice on screen
  var busy = false;
  var lastInputAt = 0;
  var composing = false;
  var checking = Promise.resolve();
  var bar = null;
  var reloadButton = null;
  var noteLine = null;

  function store(kind) {
    try { return kind === 'local' ? window.localStorage : window.sessionStorage; } catch (_) { return null; }
  }
  function getItem(kind, key) {
    var s = store(kind);
    try { return s ? s.getItem(key) : null; } catch (_) { return null; }
  }
  function setItem(kind, key, value) {
    var s = store(kind);
    try { if (s) s.setItem(key, value); } catch (_) {}
  }

  // Request/response over a MessageChannel. Rejects if the worker does not answer in time.
  function ask(worker, message) {
    return new Promise(function (resolve, reject) {
      var channel = new MessageChannel();
      var timer = setTimeout(function () { reject(new Error('no reply')); }, REPLY_TIMEOUT_MS);
      channel.port1.onmessage = function (event) { clearTimeout(timer); resolve(event.data); };
      worker.postMessage(message, [channel.port2]);
    });
  }

  // The build a worker serves, validated: only a well-formed RD_BUILD reply counts.
  function buildOf(worker) {
    return ask(worker, { type: 'RD_BUILD?' }).then(function (data) {
      return data && data.type === 'RD_BUILD' && typeof data.build === 'string' && HEX16.test(data.build)
        ? data.build : '';
    });
  }

  function randomHex() {
    var bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.prototype.map.call(bytes, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
  }

  // Hidden while the recovery dialog or the reconnect banner shows, while offline, or while a
  // text field has focus. Event-driven rechecks only; no timers.
  function hiddenNow() {
    var recovery = document.getElementById('relaydeck-session-recovery');
    if (recovery && recovery.open !== false) return true;
    if (document.getElementById('relaydeck-reconnect-banner')) return true;
    if (navigator.onLine === false) return true;
    var active = document.activeElement;
    return !!active && (/^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName) || !!active.isContentEditable);
  }

  function button(label) {
    var el = document.createElement('button');
    el.type = 'button';
    el.textContent = label;
    el.style.cssText = 'min-height:44px;min-width:88px;padding:0 14px;border:1px solid #fff;' +
      'border-radius:8px;background:transparent;color:#fff;font:inherit';
    return el;
  }

  function ensureBar() {
    if (bar) return bar;
    bar = document.createElement('div');
    bar.id = 'rd-update-bar';
    bar.setAttribute('role', 'status');
    bar.setAttribute('aria-live', 'polite');
    bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483644;display:none;' +
      'align-items:center;justify-content:center;flex-wrap:wrap;gap:12px;box-sizing:border-box;' +
      'padding:10px 16px calc(10px + env(safe-area-inset-bottom));background:#130F26;color:#fff;' +
      'font:14px system-ui,sans-serif';
    var text = document.createElement('span');
    text.textContent = 'Update ready';
    reloadButton = button('Reload');
    reloadButton.addEventListener('click', onReload);
    var later = button('Later');
    later.addEventListener('click', onLater);
    noteLine = document.createElement('span');
    noteLine.id = 'rd-update-note';
    bar.appendChild(text);
    bar.appendChild(reloadButton);
    bar.appendChild(later);
    bar.appendChild(noteLine);
    document.body.appendChild(bar);
    return bar;
  }

  function updateVisibility() {
    if (!offered) {
      if (bar) bar.style.display = 'none';
      return;
    }
    ensureBar().style.display = hiddenNow() ? 'none' : 'flex';
  }

  function note(text) {
    ensureBar();
    noteLine.textContent = text || '';
  }

  function onLater() {
    if (busy || !offered) return;
    setItem('session', DISMISS_KEY, offered.build);
    offered = null;
    updateVisibility();
  }

  // An update is real only when a worker already controls this page (not the first install) and
  // the waiting worker serves a different build than this page.
  function consider(worker) {
    if (!worker || busy || !navigator.serviceWorker.controller) return;
    if (offered && offered.worker === worker) return;
    buildOf(worker).then(function (build) {
      if (!build || build === PAGE_BUILD || worker.state === 'redundant') return;
      if (getItem('session', DISMISS_KEY) === build) return;
      if (busy) return;
      offered = { build: build, worker: worker };
      note('');
      updateVisibility();
    }).catch(function () {});
  }

  function watch(reg) {
    registration = reg;
    if (reg.waiting) consider(reg.waiting);
    reg.addEventListener('updatefound', function () {
      var installing = reg.installing;
      if (!installing) return;
      installing.addEventListener('statechange', function () {
        if (installing.state === 'installed') consider(installing);
      });
    });
  }

  // Quiescence before activation: no input or composition for QUIET_MS, capped at QUIET_CAP_MS.
  function waitForQuiet() {
    return new Promise(function (resolve) {
      var deadline = Date.now() + QUIET_CAP_MS;
      (function check() {
        var idle = Date.now() - lastInputAt;
        if ((!composing && idle >= QUIET_MS) || Date.now() >= deadline) return resolve();
        setTimeout(check, Math.max(50, QUIET_MS - idle));
      })();
    });
  }

  function fail(text) {
    busy = false;
    if (reloadButton) reloadButton.disabled = false;
    note(text);
  }

  function stillOffered(target) {
    return offered === target && registration !== null && registration.waiting === target.worker &&
      target.worker.state === 'installed';
  }

  // Activation for one consent: the worker must accept (single RelayDeck window). The page then
  // waits for that worker to become active (no clients.claim, so no controllerchange for this page),
  // verifies the build, stamps the watchdog cooldown and reloads exactly once.
  function activate(consent) {
    var worker = consent.worker;
    var timer = null;
    var listening = true;

    function stop() {
      listening = false;
      clearTimeout(timer);
      worker.removeEventListener('statechange', onState);
      navigator.serviceWorker.removeEventListener('controllerchange', onController);
    }
    function activated() {
      if (!listening || consent.consumed) return;
      stop();
      consent.consumed = true;
      buildOf(worker).then(function (seen) {
        if (seen !== consent.build) return fail('Update check failed. Tap Reload to try again.');
        setItem('session', RELOAD_KEY, String(Date.now()));
        location.reload();
      }).catch(function () { fail('Update check failed. Tap Reload to try again.'); });
    }
    function onState() {
      if (!listening) return;
      if (worker.state === 'activated') activated();
      else if (worker.state === 'redundant') { stop(); fail('Update was replaced. Tap Reload to check again.'); }
    }
    function onController() { activated(); }

    worker.addEventListener('statechange', onState);
    navigator.serviceWorker.addEventListener('controllerchange', onController);
    timer = setTimeout(function () {
      if (!listening) return;
      stop();
      fail('Update did not finish. Tap Reload to try again.');
    }, ACTIVATE_TIMEOUT_MS);

    ask(worker, { type: 'RD_ACTIVATE', build: consent.build, nonce: consent.nonce }).then(function (reply) {
      if (!listening) return;
      if (reply && reply.type === 'RD_ACTIVATE_ACCEPTED' && reply.nonce === consent.nonce) return;
      stop();
      if (reply && reply.type === 'RD_ACTIVATE_REFUSED') {
        fail('Close other RelayDeck windows to update');
      } else {
        fail('Update did not start. Tap Reload to try again.');
      }
    }).catch(function () {
      if (!listening) return;
      stop();
      fail('Update did not start. Tap Reload to try again.');
    });
  }

  function onReload() {
    if (busy || !offered) return;
    busy = true;
    reloadButton.disabled = true;
    note('');
    var target = offered;
    waitForQuiet().then(function () {
      if (!stillOffered(target)) return fail('Update is no longer available. Tap Reload to check again.');
      activate({ build: target.build, worker: target.worker, nonce: randomHex(), consumed: false });
    }).catch(function () { fail('Update did not start. Tap Reload to try again.'); });
  }

  // Foreground checks: visibility-driven only, at most once per CHECK_GAP_MS, serialized.
  function checkForUpdate() {
    if (!registration) return;
    var last = Number(getItem('local', CHECK_KEY)) || 0;
    if (Date.now() - last < CHECK_GAP_MS) return;
    setItem('local', CHECK_KEY, String(Date.now()));
    checking = checking.then(function () {
      return registration.update().then(function () {
        if (registration.waiting) consider(registration.waiting);
      }).catch(function () {});
    });
  }

  function trackInput() {
    document.addEventListener('input', function () { lastInputAt = Date.now(); }, true);
    document.addEventListener('keydown', function () { lastInputAt = Date.now(); }, true);
    document.addEventListener('compositionstart', function () { composing = true; }, true);
    document.addEventListener('compositionupdate', function () { lastInputAt = Date.now(); }, true);
    document.addEventListener('compositionend', function () { composing = false; lastInputAt = Date.now(); }, true);
  }

  // The notification banner. It is shown only while action is needed, and never over
  // content: it sits inline at the top of the page, so it takes its own height. The cases:
  //  - "Get notifications" when permission is "default" and push is supported (a tap asks);
  //  - "Turn on" when permission is granted but this device is not subscribed (after Turn off in
  //    Settings, or after the companion's key was rotated; no permission prompt is needed);
  //  - the Home Screen hint on iOS outside the home-screen app.
  // Once the server reports this device subscribed, the banner stays hidden (also after reload).
  // "×" hides it until RelayDeck is next opened, or until this device's choice in Settings
  // changes: a longer memory would leave no way back to the one control that can ask for
  // permission, since Settings runs on the server and cannot. A choice of none shows no bar.
  var PUSH_KEY_PATH = '/v1/push/web/key';
  var PUSH_SUB_PATH = '/v1/push/web/subscription';
  var PUSH_LEGACY_DISMISS_KEY = 'rd-push-dismissed-until';  // an older build hid it for 30 days
  var pushChoice = '';            // this device's choice as last reported, e.g. "blocked,done"
  var pushDismissedFor = null;    // the choice the bar was closed under, or null when not closed
  var pushBar = null;

  function isIos() {
    return /iPad|iPhone|iPod/.test(navigator.userAgent) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  }
  function isStandalone() {
    return window.navigator.standalone === true ||
      !!(window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
  }
  function pushSupported() {
    return 'PushManager' in window && 'Notification' in window && 'serviceWorker' in navigator;
  }
  function urlBase64ToBytes(text) {
    var padded = text + '==='.slice((text.length + 3) % 4);
    var raw = atob(padded.replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, function (c) { return c.charCodeAt(0); });
  }
  function forgetLegacyDismissal() {
    try { window.localStorage.removeItem(PUSH_LEGACY_DISMISS_KEY); } catch (_) {}
  }
  // The server's answer for this device: { publicKey, csrf, subscribed, choice }, or null when
  // not paired.
  function fetchKey() {
    return fetch(PUSH_KEY_PATH, { credentials: 'same-origin', cache: 'no-store' }).then(function (response) {
      if (!response.ok) return null;  // 401: not paired, so there is no banner
      return response.json().then(function (doc) {
        return doc && typeof doc.publicKey === 'string' && typeof doc.csrf === 'string' &&
          typeof doc.subscribed === 'boolean' && Array.isArray(doc.choice) &&
          doc.choice.every(function (item) { return typeof item === 'string'; }) ? doc : null;
      });
    });
  }
  function ensurePushBar() {
    if (pushBar) return pushBar;
    pushBar = document.createElement('div');
    pushBar.id = 'rd-push-bar';
    pushBar.setAttribute('role', 'status');
    pushBar.setAttribute('aria-live', 'polite');
    // Pinned over the top edge: the app's full-screen view is drawn above ordinary page flow, so
    // an inline bar would sit hidden underneath it. It clears the notch and leaves once handled.
    pushBar.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:2147483645;' +
      'display:none;align-items:center;justify-content:center;flex-wrap:wrap;gap:12px;' +
      'box-sizing:border-box;padding:calc(10px + env(safe-area-inset-top)) 16px 10px;' +
      'background:#130F26;color:#fff;font:14px system-ui,sans-serif;' +
      'box-shadow:0 2px 8px rgba(0,0,0,0.45)';
    document.body.appendChild(pushBar);
    return pushBar;
  }
  function hidePushBar() {
    if (pushBar) pushBar.style.display = 'none';
  }
  // text: the message. actions: buttons to show before the dismiss control.
  function showPushBar(text, actions) {
    if (pushDismissedFor === pushChoice) return hidePushBar();
    var bar = ensurePushBar();
    bar.textContent = '';
    var label = document.createElement('span');
    label.textContent = text;
    bar.appendChild(label);
    actions.forEach(function (action) { bar.appendChild(action); });
    var close = button('×');
    close.setAttribute('aria-label', 'Hide until RelayDeck is opened again');
    close.addEventListener('click', function () {
      pushDismissedFor = pushChoice;
      hidePushBar();
    });
    bar.appendChild(close);
    bar.style.display = 'flex';
  }

  // The browser's subscription goes to the server with the session cookie and the CSRF token.
  function putSubscription(subscription, csrf) {
    var json = subscription.toJSON();
    var body = { endpoint: json.endpoint, keys: { p256dh: json.keys.p256dh, auth: json.keys.auth } };
    return fetch(PUSH_SUB_PATH, {
      method: 'PUT',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-RelayDeck-CSRF': csrf },
      body: JSON.stringify(body),
    }).then(function (response) {
      if (!response.ok) throw new Error('subscription refused');
    });
  }

  // Permission is already granted (or was just granted by a tap): subscribe, then hide for good.
  function subscribeNow(keyDoc) {
    return navigator.serviceWorker.ready.then(function (reg) {
      return reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToBytes(keyDoc.publicKey),
      });
    }).then(function (subscription) {
      return putSubscription(subscription, keyDoc.csrf);
    }).then(function () {
      hidePushBar();
    }, function () {
      showPushBar('Could not turn on notifications. Try again later.', []);
    });
  }

  function enablePush(keyDoc) {
    return Notification.requestPermission().then(function (permission) {
      if (permission === 'granted') return subscribeNow(keyDoc);
      hidePushBar();  // denied or dismissed by the browser: the browser's setting is the authority
    });
  }

  // Pairing and the Settings choice both change inside the running app without a reload, so
  // the answer is checked again later (one small request): a minute while unpaired, 15 seconds
  // while this device could still be offered notifications. Never while hidden or subscribed.
  var PUSH_RECHECK_MS = 60 * 1000;
  var PUSH_CHOICE_RECHECK_MS = 15 * 1000;
  var pushRecheck = null;
  var pushStarted = false;  // set once the service worker is registered
  function recheckPush(ms) {
    if (pushRecheck) return;
    pushRecheck = setTimeout(function () {
      pushRecheck = null;
      // Coming back to the foreground re-checks anyway (visibilitychange).
      if (document.visibilityState !== 'hidden') pushBanner();
    }, ms);
  }
  function pushBanner() {
    // Through a promise so even a synchronous failure lands in the catch below, never in the
    // page's event handlers.
    return Promise.resolve().then(fetchKey).then(function (keyDoc) {
      if (!keyDoc) {
        recheckPush(PUSH_RECHECK_MS);
        return hidePushBar();  // not paired: no banner
      }
      pushChoice = keyDoc.choice.slice().sort().join(',');
      if (keyDoc.subscribed) return hidePushBar();
      recheckPush(PUSH_CHOICE_RECHECK_MS);
      if (!pushChoice) return hidePushBar();  // the device chose none: nothing to offer
      // iPhone and iPad offer web push only to the Home Screen app; a Safari tab has no
      // Notification API at all, so this hint must come before that check.
      if (isIos() && !isStandalone()) {
        if (!keyDoc.subscribed) {
          showPushBar('To get notifications, add RelayDeck to your Home Screen ' +
            '(Share → Add to Home Screen) and open it from there', []);
        }
        return;
      }
      if (!('Notification' in window)) return;
      var permission = Notification.permission;
      if (!pushSupported() || keyDoc.subscribed) return hidePushBar();
      if (permission === 'default') {
        showPushBar('Get notifications when an agent needs you', [
          (function () {
            var el = button('Get notifications');
            el.addEventListener('click', function () { enablePush(keyDoc); });
            return el;
          })(),
        ]);
      } else if (permission === 'granted') {
        showPushBar('Notifications are off for this device', [
          (function () {
            var el = button('Turn on');
            el.addEventListener('click', function () { subscribeNow(keyDoc); });
            return el;
          })(),
        ]);
      } else if (permission === 'denied') {
        // The browser will not ask again; only the system setting can allow it.
        showPushBar(isIos()
          ? 'Notifications are blocked. Allow them in Settings → Notifications → RelayDeck'
          : 'Notifications are blocked. Allow them in this site\'s browser settings', []);
      }
    }).catch(function () {});
  }

  window.addEventListener('load', function () {
    forgetLegacyDismissal();
    trackInput();
    document.addEventListener('focusin', updateVisibility, true);
    document.addEventListener('focusout', function () { setTimeout(updateVisibility, 0); }, true);
    window.addEventListener('online', updateVisibility);
    window.addEventListener('offline', updateVisibility);
    if (window.MutationObserver) {
      new MutationObserver(function () { if (offered) updateVisibility(); })
        .observe(document.body, { childList: true, subtree: true });
    }
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState !== 'visible') return;
      checkForUpdate();
      // Back from the system settings or another app: permission or pairing may have changed.
      if (pushStarted) pushBanner();
    });
    window.addEventListener('pageshow', checkForUpdate);
    navigator.serviceWorker.register('/flutter_service_worker.js', { scope: '/', updateViaCache: 'none' })
      .then(function (reg) { watch(reg); pushStarted = true; return pushBanner(); })
      .catch(function (error) { console.debug('service worker registration failed', error); });
  });
})();
