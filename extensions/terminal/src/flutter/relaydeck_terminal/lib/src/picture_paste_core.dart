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
    required this.onText,
    this.picturesEnabled = _always,
  });

  static bool _always() => true;

  final String ownerId;

  /// Synchronous: true only while Flutter focus is inside this region.
  final bool Function() eligible;

  final void Function(String requestId, String mime, int size) onOffer;
  final void Function(String reason) onReject;

  /// Text the focused terminal of this region pastes through Python's policy.
  final void Function(String text) onText;

  /// False while Python cannot take pictures: files in a paste are then ignored.
  final bool Function() picturesEnabled;
}

/// Largest text one paste may carry (characters); Python checks it again.
const int maxPasteText = 1024 * 1024;

enum PasteAction {
  /// Nothing of RelayDeck's owns the paste: the browser keeps its default.
  ignore,

  /// One acceptable picture, offered to Python.
  offer,

  /// Explained to the user; nothing is inserted.
  reject,

  /// Text for the focused terminal's reviewed paste.
  terminal,

  /// Text inserted into the focused Flutter text field at its caret.
  field,
}

class PasteDecision {
  const PasteDecision(this.action, {this.reason = ''});

  final PasteAction action;
  final String reason;

  /// The browser's own paste is suppressed for every action but [ignore], so the
  /// text can never land twice.
  bool get takesEvent => action != PasteAction.ignore;
}

/// Decides one paste from facts alone. Pictures win over text while Python can
/// take them; a text paste belongs to the focused terminal, else to the focused
/// text field; one without text is refused with a reason when an input owns it.
PasteDecision decidePaste({
  required PasteClassification pictures,
  required String text,
  required bool terminalFocused,
  required bool fieldFocused,
  required bool pictureBusy,
}) {
  final focused = terminalFocused || fieldFocused;
  final trimmed = text.trim();
  // Office apps add a picture of the selection beside its text; inside an input the text
  // wins. A Finder file copy carries only the file's name as text, which never lands.
  final textWins = focused &&
      trimmed.isNotEmpty &&
      pictures.verdict == PasteVerdict.accept &&
      trimmed != pictures.candidate?.name;
  if (focused && (pictures.verdict == PasteVerdict.none || textWins) &&
      text.length > maxPasteText) {
    return const PasteDecision(PasteAction.reject, reason: 'text_too_large');
  }
  if (textWins) {
    return PasteDecision(
        terminalFocused ? PasteAction.terminal : PasteAction.field);
  }
  switch (pictures.verdict) {
    case PasteVerdict.accept:
      return pictureBusy
          ? const PasteDecision(PasteAction.reject, reason: 'busy')
          : const PasteDecision(PasteAction.offer);
    case PasteVerdict.multiple:
      return const PasteDecision(PasteAction.reject, reason: 'multiple');
    case PasteVerdict.unsupported:
      return const PasteDecision(PasteAction.reject, reason: 'unsupported');
    case PasteVerdict.empty:
      return const PasteDecision(PasteAction.reject, reason: 'empty');
    case PasteVerdict.tooLarge:
      return const PasteDecision(PasteAction.reject, reason: 'too_large');
    case PasteVerdict.none:
      if (!terminalFocused && !fieldFocused) {
        return const PasteDecision(PasteAction.ignore);
      }
      if (text.isEmpty) {
        return const PasteDecision(PasteAction.reject, reason: 'no_text');
      }
      return PasteDecision(
          terminalFocused ? PasteAction.terminal : PasteAction.field);
  }
}

/// The one place that decides which region, if any, owns a paste.
class PicturePasteDispatcher {
  final List<PictureRegion> _regions = [];

  void Function()? onChanged;

  /// Set by the focused terminal: a text paste then goes to the terminal's own
  /// reviewed paste in Python instead of the browser's insertion.
  bool Function()? terminalFocused;

  bool get terminalOwnsPaste => terminalFocused?.call() ?? false;

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
