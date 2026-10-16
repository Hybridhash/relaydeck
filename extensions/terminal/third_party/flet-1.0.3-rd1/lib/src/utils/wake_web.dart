import 'dart:js_interop';

import 'package:web/web.dart' as web;

/// Browser return-from-background sources for the root app's backend.
class WakeSources {
  /// Forwards `visibilitychange` to visible, `pageshow` from the
  /// back-forward cache, and `online`. Returns a function that removes the
  /// listeners. Deliberately no `focus`: it fires on every tap-related
  /// focus change.
  static void Function() install({
    required void Function(String source) onWake,
  }) {
    final onVisibilityChange = ((web.Event _) {
      if (!web.document.hidden) {
        onWake("visible");
      }
    }).toJS;
    final onPageShow = ((web.Event event) {
      if ((event as web.PageTransitionEvent).persisted) {
        onWake("pageshow");
      }
    }).toJS;
    final onOnline = ((web.Event _) {
      onWake("online");
    }).toJS;
    web.document.addEventListener("visibilitychange", onVisibilityChange);
    web.window.addEventListener("pageshow", onPageShow);
    web.window.addEventListener("online", onOnline);
    return () {
      web.document.removeEventListener("visibilitychange", onVisibilityChange);
      web.window.removeEventListener("pageshow", onPageShow);
      web.window.removeEventListener("online", onOnline);
    };
  }
}
