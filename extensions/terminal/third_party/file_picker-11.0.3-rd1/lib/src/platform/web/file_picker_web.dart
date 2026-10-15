import 'dart:async';
import 'dart:js_interop';
import 'dart:typed_data';

import 'package:file_picker/src/api/file_picker_types.dart';
import 'package:file_picker/src/api/file_picker_result.dart';
import 'package:file_picker/src/api/platform_file.dart';
import 'package:file_picker/src/platform/file_picker_platform_interface.dart';
import 'package:flutter_web_plugins/flutter_web_plugins.dart';
import 'package:path/path.dart' as p;
import 'package:web/web.dart';

class FilePickerWeb extends FilePickerPlatform {
  late Element _target;
  final String _kFilePickerInputsDomId = '__file_picker_web-file-input';

  final int _readStreamChunkSize = 1000 * 1000; // 1 MB

  // Session of the most recent pick attempt. Supersession retires the
  // previous session synchronously (listeners off, future resolved as
  // cancelled) before the next input is installed, so an abandoned pick
  // can never hang or leak window listeners. Completion paths only ever
  // remove their own input. (4.0.1 has no supersede path — its
  // clearer orphans the old future the same way 11.0.3 did.)
  _PickSession? _activeSession;

  FilePickerWeb._() {
    _target = _ensureInitialized(_kFilePickerInputsDomId);
  }

  static void registerWith(Registrar registrar) {
    FilePickerPlatform.instance = FilePickerWeb._();
  }

  /// Initializes a DOM container where we can host input elements.
  Element _ensureInitialized(String id) {
    Element? target = document.querySelector('#$id');
    if (target == null) {
      final Element targetElement = document.createElement(
        'flt-file-picker-inputs',
      )..id = id;

      // RD1: `children.add` does not exist on HTMLCollection in
      // package:web; it resolved to the js_interop `+` operator and
      // silently never inserted the container. WebKit drops `change`
      // for inputs outside the document, so the pick hung on Safari.
      document.body!.appendChild(targetElement);
      target = targetElement;
    }
    return target;
  }

  @override
  Future<FilePickerResult?> pickFiles({
    String? dialogTitle,
    String? initialDirectory,
    FileType type = FileType.any,
    List<String>? allowedExtensions,
    bool allowMultiple = false,
    Function(FilePickerStatus)? onFileLoading,
    bool withData = true,
    bool withReadStream = false,
    bool lockParentWindow = false,
    bool readSequential = false,
    bool cancelUploadOnWindowBlur = true,
    int compressionQuality = 0,
  }) async {
    if (type != FileType.custom && (allowedExtensions?.isNotEmpty ?? false)) {
      throw Exception(
          'You are setting a type [$type]. Custom extension filters are only allowed with FileType.custom, please change it or remove filters.');
    }

    Completer<List<PlatformFile>?>? filesCompleter =
        Completer<List<PlatformFile>?>();

    String accept = _fileType(type, allowedExtensions);
    HTMLInputElement uploadInput = HTMLInputElement();
    uploadInput.type = 'file';
    uploadInput.draggable = true;
    uploadInput.multiple = allowMultiple;
    uploadInput.accept = accept;
    uploadInput.style.display = 'none';

    // Settle-once guard shared by the change and cancel paths (RD1).
    bool settled = false;

    if (onFileLoading != null) {
      onFileLoading(FilePickerStatus.picking);
    }

    // Cached once and reused for both addEventListener and
    // removeEventListener: Function.toJS creates a new JS function object
    // on every call, so freshly-created handles would never match the
    // added listener and it would stay attached forever (RD1).
    late final JSFunction changeListener;
    late final JSFunction cancelListener;

    void cleanupListeners() {
      window.removeEventListener('focus', cancelListener);
      uploadInput.removeEventListener('change', changeListener);
      uploadInput.removeEventListener('cancel', cancelListener);
    }

    void removeUploadInput() {
      // Only this attempt's input, and only if no newer attempt has
      // replaced it since (RD1: superseded attempts stay hands-off).
      if (identical(_activeSession?.input, uploadInput)) {
        _activeSession = null;
        if (uploadInput.parentNode == _target) {
          _target.removeChild(uploadInput);
        }
      }
    }

    // The single idempotent settlement path. The future is
    // completed BEFORE the status listener runs, and the listener is
    // guarded, so a throwing onFileLoading can neither veto completion
    // nor break supersede/retire. Errors settle with null so the gesture
    // path still delivers an (empty) result event (RD1).
    void finish(List<PlatformFile>? files) {
      if (filesCompleter?.isCompleted ?? true) {
        return;
      }
      removeUploadInput();
      filesCompleter?.complete(files);
      if (onFileLoading != null) {
        try {
          onFileLoading(FilePickerStatus.done);
        } catch (_) {
          // Status listeners never veto completion.
        }
      }
    }

    // The single failure path: every processing failure — sync
    // read errors, FileReader error/abort, callback exceptions — lands
    // here and settles the attempt null exactly once. Later events,
    // timers and in-flight reader callbacks become no-ops via the
    // settled flag and the completer guard.
    void failAttempt() {
      settled = true;
      cleanupListeners();
      finish(null);
    }

    // This attempt's session, retired synchronously if a newer pick
    // starts while this one is still pending.
    final _PickSession session = _PickSession(
      uploadInput,
      () => failAttempt(),
    );

    // Future<void> (not void): both settle paths await this (RD1).
    Future<void> deliverSelection(FileList? files) async {
      final List<File> selected = [];
      try {
        if (files != null) {
          for (int i = 0; i < files.length; i++) {
            final File? file = files.item(i);
            if (file != null) {
              selected.add(file);
            }
          }
        }
      } catch (_) {
        failAttempt();
        return;
      }
      // An empty selection still settles; 11.0.3 hung here (RD1).
      if (selected.isEmpty) {
        finish([]);
        return;
      }

      final List<PlatformFile> pickedFiles = [];

      void addPickedFile(
        File file,
        Uint8List? bytes,
        String? path,
        Stream<List<int>>? readStream,
      ) {
        if (filesCompleter?.isCompleted ?? true) {
          return;
        }
        try {
          String? blobUrl;
          if (bytes != null && bytes.isNotEmpty) {
            final blob =
                Blob([bytes.toJS].toJS, BlobPropertyBag(type: file.type));

            blobUrl = URL.createObjectURL(blob);
          }
          pickedFiles.add(PlatformFile(
            name: file.name,
            path: path ?? blobUrl,
            size: bytes != null ? bytes.length : file.size,
            bytes: bytes,
            readStream: readStream,
          ));
        } catch (_) {
          failAttempt();
          return;
        }

        if (pickedFiles.length >= selected.length) {
          finish(pickedFiles);
        }
      }

      for (final File file in selected) {
        // Retired or settled mid-read: stop, the attempt is over.
        if (filesCompleter?.isCompleted ?? true) {
          return;
        }
        try {
          if (withReadStream) {
            addPickedFile(file, null, null, _openFileReadStream(file));
            continue;
          }

          if (!withData) {
            final FileReader reader = FileReader();
            // 4.0.1 completes errored reads with null bytes; RD1 settles
            // the attempt null instead — no partial results, and no
            // error channel on Flet's gesture path. package:web
            // exposes no onError stream on FileReader, so use
            // addEventListener like 4.0.1 does.
            void failRead(Event _) => failAttempt();
            reader.addEventListener('error', failRead.toJS);
            reader.addEventListener('abort', failRead.toJS);
            reader.onLoadEnd.listen((e) {
              try {
                String? result = (reader.result as JSString?)?.toDart;
                addPickedFile(file, null, result, null);
              } catch (_) {
                failAttempt();
              }
            });
            reader.readAsDataURL(file);
            continue;
          }

          final syncCompleter = Completer<void>();
          void completeSync() {
            if (!syncCompleter.isCompleted) {
              syncCompleter.complete();
            }
          }
          final FileReader reader = FileReader();
          void failRead(Event _) {
            failAttempt();
            completeSync();
          }
          reader.addEventListener('error', failRead.toJS);
          reader.addEventListener('abort', failRead.toJS);
          reader.onLoadEnd.listen((e) {
            try {
              ByteBuffer? byteBuffer =
                  (reader.result as JSArrayBuffer?)?.toDart;
              addPickedFile(
                  file, byteBuffer?.asUint8List(), null, null);
            } catch (_) {
              failAttempt();
            } finally {
              // Always release readSequential, even on failure.
              completeSync();
            }
          });
          reader.readAsArrayBuffer(file);
          if (readSequential) {
            await syncCompleter.future;
          }
        } catch (_) {
          // Sync read errors settle the attempt null: 11.0.3
          // hung here and RD1 made a phantom file instead.
          failAttempt();
          return;
        }
      }
    }

    void changeEventListener(Event e) async {
      if (settled) {
        return;
      }
      settled = true;
      cleanupListeners();
      // 4.0.1 catches processFiles failures around completion; RD1
      // settles null through the same single path.
      try {
        await deliverSelection(uploadInput.files);
      } catch (_) {
        failAttempt();
      }
    }

    void cancelledEventListener(Event _) {
      window.removeEventListener('focus', cancelListener);

      // The focus/cancel signal can arrive before change (Safari fires
      // focus first once the dialog closes). Wait briefly for results to
      // travel from JS to Dart, then deliver any selection already on the
      // input instead of reporting a cancellation (RD1).
      Future.delayed(Duration(seconds: 1)).then((value) async {
        try {
          if (settled) {
            return;
          }
          settled = true;
          cleanupListeners();
          final FileList? files = uploadInput.files;
          if (files != null && files.length > 0) {
            await deliverSelection(files);
          } else {
            finish(null);
          }
        } catch (_) {
          failAttempt();
        }
      });
    }

    changeListener = changeEventListener.toJS;
    cancelListener = cancelledEventListener.toJS;

    // RD1: the old `uploadInput.onChange.listen(...)` duplicate is gone;
    // the cached addEventListener handle below is removed on settle.
    uploadInput.addEventListener('change', changeListener);
    uploadInput.addEventListener('cancel', cancelListener);

    if (cancelUploadOnWindowBlur) {
      // Listen focus event for cancelled
      window.addEventListener('focus', cancelListener);
    }

    // Attach synchronously inside the user gesture: WebKit only delivers
    // `change` for inputs in the document. A pending previous attempt is
    // retired synchronously first — listeners off, future resolved as
    // cancelled — so it can never hang or disturb this one.
    final _PickSession? previousSession = _activeSession;
    previousSession?.retire();
    _activeSession = session;
    _target.appendChild(uploadInput);
    uploadInput.click();

    final List<PlatformFile>? files = await filesCompleter.future;
    filesCompleter = null;

    return files == null ? null : FilePickerResult(files);
  }

  @override
  Future<String?> saveFile({
    String? dialogTitle,
    String? fileName,
    String? initialDirectory,
    FileType type = FileType.any,
    List<String>? allowedExtensions,
    Uint8List? bytes,
    bool lockParentWindow = false,
  }) async {
    if (bytes == null || bytes.isEmpty) {
      throw ArgumentError(
        'The bytes are required when saving a file on the web.',
      );
    }

    if (fileName == null || fileName.isEmpty) {
      throw ArgumentError(
        'A file name is required when saving a file on the web.',
      );
    }

    if (p.extension(fileName).isEmpty) {
      throw ArgumentError(
        'The file name should include a valid file extension.',
      );
    }

    final blob = Blob([bytes.toJS].toJS);
    final url = URL.createObjectURL(blob);

    // Start a download by using a click event on an anchor element that contains the Blob.
    HTMLAnchorElement()
      ..href = url
      ..target = 'blank' // Always open the file in a new tab.
      ..download = fileName
      ..click();

    // Release the Blob URL after the download started.
    URL.revokeObjectURL(url);
    return null;
  }

  static String _fileType(FileType type, List<String>? allowedExtensions) {
    switch (type) {
      case FileType.any:
        return '';

      case FileType.audio:
        return 'audio/*';

      case FileType.image:
        return 'image/*';

      case FileType.video:
        return 'video/*';

      case FileType.media:
        return 'video/*|image/*';

      case FileType.custom:
        return allowedExtensions!
            .fold('', (prev, next) => '${prev.isEmpty ? '' : '$prev,'} .$next');
    }
  }

  Stream<List<int>> _openFileReadStream(File file) async* {
    final reader = FileReader();

    int start = 0;
    while (start < file.size) {
      final end = start + _readStreamChunkSize > file.size
          ? file.size
          : start + _readStreamChunkSize;
      final blob = file.slice(start, end);
      reader.readAsArrayBuffer(blob);
      await EventStreamProviders.loadEvent.forTarget(reader).first;
      final JSAny? readerResult = reader.result;
      if (readerResult == null) {
        continue;
      }

      // Handle the ArrayBuffer type. This maps to a `ByteBuffer` in Dart.
      if (readerResult.isA<JSArrayBuffer>()) {
        yield (readerResult as JSArrayBuffer).toDart.asUint8List();
        start += _readStreamChunkSize;
        continue;
      }

      if (readerResult.isA<JSArray>()) {
        // Assume this is a List<int>.
        yield (readerResult as JSArray).toDart.cast<int>();
        start += _readStreamChunkSize;
      }
    }
  }
}

/// One pending web pick: its input plus its synchronous retirement.
/// Supersession retires the previous session before installing the next
/// input; retirement unregisters that attempt's cached listeners and
/// resolves it as cancelled, and its later events/timers are ignored.
/// (4.0.1 has no supersede path.)
class _PickSession {
  _PickSession(this.input, this._retireAttempt);

  /// This attempt's attached input.
  final HTMLInputElement input;

  final void Function() _retireAttempt;

  bool _retired = false;

  /// Synchronously retire this attempt. Idempotent.
  void retire() {
    if (_retired) {
      return;
    }
    _retired = true;
    _retireAttempt();
  }
}
