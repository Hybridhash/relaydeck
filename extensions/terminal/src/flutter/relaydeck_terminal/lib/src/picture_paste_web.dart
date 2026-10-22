import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart' as web;

import 'field_paste.dart';
import 'picture_paste_core.dart';

const _pendingLifetime = Duration(seconds: 150);

class _Pending {
  _Pending(this.owner, this.file, this.timer);

  /// The region that took the paste; only it may upload or abort this request.
  final String owner;
  final web.File file;
  final Timer timer;
  web.AbortController? abort;
}

/// One document-level paste listener shared by every region. It takes the
/// picture synchronously from the real paste event and keeps the Blob in the
/// browser: only a request id and metadata ever reach Python.
class PicturePasteSource {
  PicturePasteSource._() {
    dispatcher.onChanged = _syncListener;
  }

  static final PicturePasteSource shared = PicturePasteSource._();

  final PicturePasteDispatcher dispatcher = PicturePasteDispatcher();
  final Map<String, _Pending> _pending = {};
  final PendingPictureLedger _ledger = PendingPictureLedger();
  int _counter = 0;
  JSFunction? _listener;

  bool get supported => true;

  void _syncListener() {
    if (!dispatcher.isEmpty && _listener == null) {
      final listener = _onPaste.toJS;
      _listener = listener;
      web.document.addEventListener(
        'paste',
        listener,
        web.AddEventListenerOptions(capture: true),
      );
    } else if (dispatcher.isEmpty && _listener != null) {
      web.document.removeEventListener(
        'paste',
        _listener,
        web.EventListenerOptions(capture: true),
      );
      _listener = null;
      abortAll();
    }
  }

  void _onPaste(web.ClipboardEvent event) {
    final region = dispatcher.focusedRegion();
    if (region == null) return;
    final data = event.clipboardData;
    if (data == null) return;

    // File references are taken here, synchronously; items are the primary list
    // and `files` only a fallback so one file is never counted twice. While
    // Python cannot take pictures the files are not looked at.
    final files = <web.File>[];
    if (region.picturesEnabled()) {
      final items = data.items;
      for (var i = 0; i < items.length; i++) {
        final item = items[i];
        if (item.kind != 'file') continue;
        final file = item.getAsFile();
        if (file != null) files.add(file);
      }
      if (files.isEmpty) {
        final list = data.files;
        for (var i = 0; i < list.length; i++) {
          final file = list.item(i);
          if (file != null) files.add(file);
        }
      }
    }

    final candidates = [
      for (final file in files)
        PasteCandidate(mime: file.type, size: file.size, name: file.name),
    ];
    final result = classifyPaste(candidates);
    final field = focusedEditable();
    final text = data.getData('text/plain');
    final decision = decidePaste(
      pictures: result,
      text: text,
      terminalFocused: dispatcher.terminalOwnsPaste,
      fieldFocused: field != null,
      pictureBusy: _ledger.occupied,
    );
    if (!decision.takesEvent) return;
    // One insertion only: the browser's own paste never runs for a taken event,
    // and Flutter's paste shortcut was already left to the browser.
    event.preventDefault();
    event.stopImmediatePropagation();
    switch (decision.action) {
      case PasteAction.ignore:
        return;
      case PasteAction.reject:
        // A refused paste is explained; a filename never lands in the field.
        region.onReject(decision.reason);
        return;
      case PasteAction.terminal:
        region.onText(text);
        return;
      case PasteAction.field:
        if (!pasteIntoField(field!, text)) region.onReject('no_text');
        return;
      case PasteAction.offer:
        final candidate = result.candidate!;
        final file = files[candidates.indexWhere((c) =>
            c.size == candidate.size &&
            c.mime == candidate.mime &&
            c.name == candidate.name)];
        final requestId = 'p${DateTime.now().microsecondsSinceEpoch}-${_counter++}';
        _ledger.claim(requestId, region.ownerId);
        _retain(requestId, file, region.ownerId);
        region.onOffer(requestId, candidate.normalizedMime, candidate.size);
        return;
    }
  }

  void _retain(String requestId, web.File file, String owner) {
    _pending[requestId] = _Pending(
      owner,
      file,
      Timer(_pendingLifetime, () => _release(requestId)),
    );
  }

  /// PUTs the retained Blob to the grant URL with the page's own credentials.
  /// Returns 'ok', or a short failure word; never a body, header or URL.
  Future<String> upload(
      String requestId, String url, int expiresMs, String owner) async {
    final pending = _pending[requestId];
    if (pending == null || !_ledger.owns(requestId, owner)) return 'gone';
    final target = resolvePictureUploadUrl(url, Uri.base);
    if (target == null) {
      _release(requestId);
      return 'bad_url';
    }
    final controller = web.AbortController();
    pending.abort = controller;
    final budget = Duration(milliseconds: expiresMs.clamp(1000, 120000));
    final deadline = Timer(budget, () => controller.abort());
    try {
      final init = web.RequestInit(
        method: 'PUT',
        body: pending.file,
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        signal: controller.signal,
      );
      final response = await web.window.fetch(target.toString().toJS, init).toDart;
      return response.status == 204 ? 'ok' : 'rejected';
    } catch (_) {
      return _pending.containsKey(requestId) ? 'network' : 'aborted';
    } finally {
      deadline.cancel();
      _release(requestId);
    }
  }

  /// Releases a request on behalf of its owning region only.
  void abort(String requestId, String owner) {
    if (!_ledger.owns(requestId, owner)) return;
    _release(requestId);
  }

  /// Releases every request a disposed region still holds, and no one else's.
  void abortOwner(String owner) {
    for (final id in _ledger.releaseOwner(owner)) {
      _release(id);
    }
  }

  void _release(String requestId) {
    _ledger.release(requestId);
    final pending = _pending.remove(requestId);
    if (pending == null) return;
    pending.timer.cancel();
    pending.abort?.abort();
  }

  void abortAll() {
    for (final id in _pending.keys.toList()) {
      _release(id);
    }
  }
}
