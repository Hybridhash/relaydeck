/// Non-web wake sources: nothing to listen to. Tests call `wake` directly.
class WakeSources {
  /// No-op, kept so the backend installs unconditionally.
  static void Function() install({
    required void Function(String source) onWake,
  }) {
    return () {};
  }
}
