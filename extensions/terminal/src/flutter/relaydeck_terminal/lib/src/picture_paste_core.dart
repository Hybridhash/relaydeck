// Browser-independent rules for attaching a clipboard picture: what counts as a
// candidate, which paste owns the picture, and how a single offer is shaped.

const int kMaxPicturePasteBytes = 10 * 1024 * 1024;

const _acceptedMimes = {'image/png', 'image/jpeg', 'image/webp'};
const _imageExtensions = {
  'png',
  'jpg',
  'jpeg',
  'webp',
  'gif',
  'bmp',
  'tif',
  'tiff',
  'heic',
  'heif',
  'avif',
  'svg',
};

enum PasteVerdict {
  /// Nothing picture-like: the paste is text and keeps its existing handling.
  none,

  /// Exactly one acceptable picture.
  accept,

  /// More than one picture in one paste.
  multiple,

  /// A picture type the server cannot take (for example HEIC).
  unsupported,

  /// A zero-byte picture.
  empty,

  /// Over the upload limit; refused before a grant is requested.
  tooLarge,
}

/// One file the clipboard exposed. `mime` may be empty or generic; only a
/// filename with an image extension then makes it a candidate, never proof.
class PasteCandidate {
  const PasteCandidate({required this.mime, required this.size, this.name = ''});

  final String mime;
  final int size;
  final String name;

  String get _mime => mime.toLowerCase().split(';').first.trim();

  bool get isImageCandidate {
    final type = _mime;
    if (type.startsWith('image/')) return true;
    if (type.isNotEmpty && type != 'application/octet-stream') return false;
    final dot = name.lastIndexOf('.');
    if (dot < 0) return false;
    return _imageExtensions.contains(name.substring(dot + 1).toLowerCase());
  }

  bool get hasAcceptedMime => _acceptedMimes.contains(_mime);

  String get normalizedMime => _mime;
}

class PasteClassification {
  const PasteClassification(this.verdict, [this.candidate]);

  final PasteVerdict verdict;
  final PasteCandidate? candidate;
}

/// Classifies the files of one paste. The caller passes the file list taken
/// from `clipboardData.items`, or `files` only when the items had none, so one
/// file is never listed twice. Identical entries are collapsed anyway.
PasteClassification classifyPaste(List<PasteCandidate> files) {
  final seen = <String>{};
  final images = <PasteCandidate>[];
  for (final file in files) {
    if (!file.isImageCandidate) continue;
    if (seen.add('${file.normalizedMime}|${file.size}|${file.name}')) {
      images.add(file);
    }
  }
  if (images.isEmpty) return const PasteClassification(PasteVerdict.none);
  if (images.length > 1) return const PasteClassification(PasteVerdict.multiple);
  final image = images.single;
  if (image.size <= 0) {
    return PasteClassification(PasteVerdict.empty, image);
  }
  if (image.size > kMaxPicturePasteBytes) {
    return PasteClassification(PasteVerdict.tooLarge, image);
  }
  // An unnamed or generic type is a candidate only; the server validates bytes.
  if (image.normalizedMime.startsWith('image/') && !image.hasAcceptedMime) {
    return PasteClassification(PasteVerdict.unsupported, image);
  }
  return PasteClassification(PasteVerdict.accept, image);
}

/// Resolves a grant URL against the page and accepts only the same-origin
/// picture content path. Returns null for anything else.
Uri? resolvePictureUploadUrl(String raw, Uri pageBase) {
  final Uri parsed;
  try {
    parsed = pageBase.resolve(raw);
  } catch (_) {
    return null;
  }
  if (parsed.scheme != pageBase.scheme ||
      parsed.host != pageBase.host ||
      parsed.port != pageBase.port ||
      parsed.hasFragment ||
      parsed.userInfo.isNotEmpty) {
    return null;
  }
  final path = RegExp(r'^/v1/pictures/[A-Za-z0-9_-]{1,64}/content$');
  if (!path.hasMatch(parsed.path)) return null;
  return parsed;
}

/// One input region a paste can belong to.
class PictureRegion {
  PictureRegion({
    required this.ownerId,
    required this.eligible,
    required this.onOffer,
    required this.onReject,
  });

  final String ownerId;

  /// Synchronous: true only while Flutter focus is inside this region.
  final bool Function() eligible;

  final void Function(String requestId, String mime, int size) onOffer;
  final void Function(String reason) onReject;
}

/// The one place that decides which region, if any, owns a paste.
class PicturePasteDispatcher {
  final List<PictureRegion> _regions = [];

  void Function()? onChanged;

  /// Set by the focused terminal: a text-only paste is handed to the terminal's own
  /// paste (bracketed framing, safe-paste policy) instead of the browser's insertion.
  bool Function(String text)? terminalPaste;

  /// Returns true when the text was taken by a terminal and the browser must not
  /// insert it a second time.
  bool pasteText(String text) {
    final take = terminalPaste;
    if (take == null || text.isEmpty) return false;
    return take(text);
  }

  void Function() register(PictureRegion region) {
    _regions.add(region);
    onChanged?.call();
    var disposed = false;
    return () {
      if (disposed) return;
      disposed = true;
      _regions.remove(region);
      onChanged?.call();
    };
  }

  bool get isEmpty => _regions.isEmpty;

  int get regionCount => _regions.length;

  /// The single focused region; two focused regions mean no owner, so a paste
  /// is never offered to the wrong terminal.
  PictureRegion? focusedRegion() {
    PictureRegion? owner;
    for (final region in _regions) {
      if (!region.eligible()) continue;
      if (owner != null) return null;
      owner = region;
    }
    return owner;
  }

  /// True while the paste shortcut should be left to the browser so the paste
  /// event can classify the payload first.
  bool get shortcutHandoff => focusedRegion() != null;
}

/// Which request each region holds. One picture is pending at a time; a second
/// paste is refused without touching the one in progress, and only the owning
/// region may upload, abort or clean up its request.
class PendingPictureLedger {
  final Map<String, String> _owners = {};

  bool get occupied => _owners.isNotEmpty;

  /// Claims the single slot. False means a picture is already pending.
  bool claim(String requestId, String owner) {
    if (_owners.isNotEmpty) return false;
    _owners[requestId] = owner;
    return true;
  }

  bool owns(String requestId, String owner) => _owners[requestId] == owner;

  void release(String requestId) => _owners.remove(requestId);

  /// Ids held by [owner], removed; used when that region goes away.
  List<String> releaseOwner(String owner) {
    final ids = [
      for (final entry in _owners.entries)
        if (entry.value == owner) entry.key,
    ];
    ids.forEach(_owners.remove);
    return ids;
  }
}
