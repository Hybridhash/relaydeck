import 'dart:js_interop';

@JS('visualViewport')
external JSObject? get _visualViewport;

@JS('scrollY')
external JSNumber get _scrollY;

@JS('innerHeight')
external JSNumber get _innerHeight;

@JS('scrollTo')
external void _scrollTo(JSNumber x, JSNumber y);

@JS('addEventListener')
external void _addWindowListener(JSString type, JSFunction listener);

extension type _VisualViewport(JSObject _) implements JSObject {
  external JSNumber get height;
  external JSNumber get offsetTop;
  external void addEventListener(JSString type, JSFunction listener);
}

double? visualViewportHeight() {
  try {
    final viewport = _visualViewport;
    return viewport == null
        ? null
        : _VisualViewport(viewport).height.toDartDouble;
  } catch (_) {
    return null;
  }
}

/// Page scroll, visual viewport offset and height, and window height.
List<double>? pageMetrics() {
  try {
    final viewport = _visualViewport;
    if (viewport == null) return null;
    final visual = _VisualViewport(viewport);
    return [
      _scrollY.toDartDouble,
      visual.offsetTop.toDartDouble,
      visual.height.toDartDouble,
      _innerHeight.toDartDouble,
    ];
  } catch (_) {
    return null;
  }
}

/// iOS can leave the page scrolled after its keyboard or a native menu,
/// pushing the app under its input bar; the app never scrolls the page itself.
bool pinPageTop() {
  try {
    if (_scrollY.toDartDouble != 0) {
      _scrollTo(0.toJS, 0.toJS);
      return true;
    }
  } catch (_) {}
  return false;
}

final _pageWatchers = <void Function()>{};
var _pageListening = false;

/// Calls [onChange] when the page scrolls or the visual viewport moves or resizes.
void Function() watchPage(void Function() onChange) {
  _pageWatchers.add(onChange);
  if (!_pageListening) {
    _pageListening = true;
    try {
      final listener = ((JSAny? _) {
        for (final watcher in List.of(_pageWatchers)) {
          watcher();
        }
      }).toJS;
      _addWindowListener('scroll'.toJS, listener);
      final viewport = _visualViewport;
      if (viewport != null) {
        _VisualViewport(viewport).addEventListener('resize'.toJS, listener);
        _VisualViewport(viewport).addEventListener('scroll'.toJS, listener);
      }
    } catch (_) {}
  }
  return () => _pageWatchers.remove(onChange);
}
