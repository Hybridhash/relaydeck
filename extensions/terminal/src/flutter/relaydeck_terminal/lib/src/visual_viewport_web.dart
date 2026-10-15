import 'dart:js_interop';

@JS('visualViewport')
external JSObject? get _visualViewport;

extension type _VisualViewport(JSObject _) implements JSObject {
  external JSNumber get height;
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
