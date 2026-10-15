import 'picture_paste_core.dart';

/// Native builds have no browser clipboard event; the picker is the only way
/// to attach a picture there, so every entry point is inert.
class PicturePasteSource {
  PicturePasteSource._();

  static final PicturePasteSource shared = PicturePasteSource._();

  final PicturePasteDispatcher dispatcher = PicturePasteDispatcher();

  bool get supported => false;

  Future<String> upload(
          String requestId, String url, int expiresMs, String owner) async =>
      'unsupported';

  void abort(String requestId, String owner) {}

  void abortOwner(String owner) {}

  void abortAll() {}
}
