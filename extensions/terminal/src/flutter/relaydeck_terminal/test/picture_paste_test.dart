// Picture paste rules: classification, ownership and URL checks. Pure Dart, so
// they run without a browser; the real shortcut check is done in a browser.
import 'package:flutter_test/flutter_test.dart';

import '../lib/src/picture_paste_core.dart';

PasteCandidate png({int size = 1000, String name = 'image.png'}) =>
    PasteCandidate(mime: 'image/png', size: size, name: name);

PictureRegion region(String owner, bool Function() eligible,
        {List<String>? log}) =>
    PictureRegion(
      ownerId: owner,
      eligible: eligible,
      onOffer: (id, mime, size) => log?.add('offer:$owner'),
      onReject: (reason) => log?.add('reject:$owner:$reason'),
    );

void main() {
  group('classifyPaste', () {
    test('text-only has no picture', () {
      expect(classifyPaste(const []).verdict, PasteVerdict.none);
      expect(
        classifyPaste(const [
          PasteCandidate(mime: 'text/plain', size: 10, name: 'a.txt'),
        ]).verdict,
        PasteVerdict.none,
      );
    });

    test('one accepted picture is claimed', () {
      for (final mime in ['image/png', 'image/jpeg', 'image/webp']) {
        final result =
            classifyPaste([PasteCandidate(mime: mime, size: 5, name: '')]);
        expect(result.verdict, PasteVerdict.accept, reason: mime);
      }
    });

    test('mixed text and picture is the picture only', () {
      final result = classifyPaste([
        const PasteCandidate(mime: 'text/plain', size: 4, name: 'note.txt'),
        png(),
      ]);
      expect(result.verdict, PasteVerdict.accept);
    });

    test('two different pictures are rejected, not truncated', () {
      expect(
        classifyPaste([png(name: 'a.png'), png(name: 'b.png', size: 2000)])
            .verdict,
        PasteVerdict.multiple,
      );
    });

    test('the same file listed twice is one picture', () {
      expect(classifyPaste([png(), png()]).verdict, PasteVerdict.accept);
    });

    test('unsupported picture types are recognised', () {
      for (final mime in ['image/heic', 'image/heif', 'image/gif', 'image/tiff']) {
        expect(
          classifyPaste([PasteCandidate(mime: mime, size: 10, name: 'x')])
              .verdict,
          PasteVerdict.unsupported,
          reason: mime,
        );
      }
    });

    test('a generic type is a candidate only with an image filename', () {
      expect(
        classifyPaste(const [
          PasteCandidate(mime: '', size: 10, name: 'shot.png'),
        ]).verdict,
        PasteVerdict.accept,
      );
      expect(
        classifyPaste(const [
          PasteCandidate(mime: 'application/octet-stream', size: 10, name: 'x.bin'),
        ]).verdict,
        PasteVerdict.none,
      );
      expect(
        classifyPaste(const [
          PasteCandidate(mime: '', size: 10, name: 'notes'),
        ]).verdict,
        PasteVerdict.none,
      );
    });

    test('size limits are checked before any upload', () {
      expect(classifyPaste([png(size: 0)]).verdict, PasteVerdict.empty);
      expect(classifyPaste([png(size: kMaxPicturePasteBytes)]).verdict,
          PasteVerdict.accept);
      expect(classifyPaste([png(size: kMaxPicturePasteBytes + 1)]).verdict,
          PasteVerdict.tooLarge);
    });
  });

  group('PicturePasteDispatcher', () {
    test('no focused region means no owner and no handoff', () {
      final dispatcher = PicturePasteDispatcher();
      dispatcher.register(region('a', () => false));
      expect(dispatcher.focusedRegion(), isNull);
      expect(dispatcher.shortcutHandoff, isFalse);
    });

    test('exactly one focused region owns the paste', () {
      final dispatcher = PicturePasteDispatcher();
      var focusA = false;
      var focusB = true;
      dispatcher
        ..register(region('a', () => focusA))
        ..register(region('b', () => focusB));
      expect(dispatcher.focusedRegion()?.ownerId, 'b');
      focusA = true;
      focusB = false;
      expect(dispatcher.focusedRegion()?.ownerId, 'a');
    });

    test('two retained terminals never share an owner', () {
      final dispatcher = PicturePasteDispatcher();
      dispatcher
        ..register(region('terminal-1', () => true))
        ..register(region('terminal-2', () => true));
      expect(dispatcher.focusedRegion(), isNull);
      expect(dispatcher.shortcutHandoff, isFalse);
    });

    test('a disposed region is no longer eligible', () {
      final dispatcher = PicturePasteDispatcher();
      final unregister = dispatcher.register(region('a', () => true));
      expect(dispatcher.regionCount, 1);
      unregister();
      unregister();
      expect(dispatcher.regionCount, 0);
      expect(dispatcher.focusedRegion(), isNull);
    });

    test('registration changes are announced for listener management', () {
      final dispatcher = PicturePasteDispatcher();
      var changes = 0;
      dispatcher.onChanged = () => changes++;
      final unregister = dispatcher.register(region('a', () => true));
      unregister();
      expect(changes, 2);
    });

    test('text goes to a focused terminal exactly once, else stays native', () {
      final dispatcher = PicturePasteDispatcher();
      expect(dispatcher.pasteText('hello'), isFalse);
      final seen = <String>[];
      dispatcher.terminalPaste = (text) {
        seen.add(text);
        return true;
      };
      expect(dispatcher.pasteText('hello'), isTrue);
      expect(dispatcher.pasteText(''), isFalse);
      expect(seen, ['hello']);
    });
  });

  group('resolvePictureUploadUrl', () {
    final page = Uri.parse('https://mac.example.net:8445/app');

    test('accepts the same-origin content path with a grant', () {
      final url = resolvePictureUploadUrl(
          '/v1/pictures/abc123/content?grant=secret', page);
      expect(url, isNotNull);
      expect(url!.origin, 'https://mac.example.net:8445');
      expect(url.path, '/v1/pictures/abc123/content');
    });

    test('refuses another origin, scheme, port, path or credentials', () {
      for (final raw in [
        'https://evil.example.org/v1/pictures/abc/content',
        'http://mac.example.net:8445/v1/pictures/abc/content',
        'https://mac.example.net:9000/v1/pictures/abc/content',
        'https://user' '@' 'mac.example.net:8445/v1/pictures/abc/content',
        '/v1/pictures/abc/other',
        '/v1/pictures/../secret/content',
        '/v1/pictures/abc/content#frag',
        '//evil.example.org/v1/pictures/abc/content',
      ]) {
        expect(resolvePictureUploadUrl(raw, page), isNull, reason: raw);
      }
    });
  });

  group('PendingPictureLedger', () {
    test('a second paste is refused while one is pending', () {
      final ledger = PendingPictureLedger();
      expect(ledger.claim('a', 'composer'), isTrue);
      expect(ledger.occupied, isTrue);
      expect(ledger.claim('b', 'terminal'), isFalse);
      expect(ledger.owns('a', 'composer'), isTrue);
      expect(ledger.owns('b', 'terminal'), isFalse);
    });

    test('only the owning region may act on a request', () {
      final ledger = PendingPictureLedger();
      ledger.claim('a', 'composer');
      expect(ledger.owns('a', 'terminal'), isFalse);
      expect(ledger.releaseOwner('terminal'), isEmpty);
      expect(ledger.occupied, isTrue);
      expect(ledger.releaseOwner('composer'), ['a']);
      expect(ledger.occupied, isFalse);
    });

    test('the slot frees on release and a new paste is distinct', () {
      final ledger = PendingPictureLedger();
      ledger.claim('a', 'composer');
      ledger.release('a');
      expect(ledger.claim('b', 'composer'), isTrue);
    });
  });
}
