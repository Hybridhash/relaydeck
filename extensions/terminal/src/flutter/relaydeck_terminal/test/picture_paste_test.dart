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
      onText: (text) => log?.add('text:$owner:$text'),
    );

PasteDecision decide({
  List<PasteCandidate> files = const [],
  String text = '',
  bool terminal = false,
  bool field = false,
  bool busy = false,
}) =>
    decidePaste(
      pictures: classifyPaste(files),
      text: text,
      terminalFocused: terminal,
      fieldFocused: field,
      pictureBusy: busy,
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

    test('a terminal owns the paste only while it says it has focus', () {
      final dispatcher = PicturePasteDispatcher();
      expect(dispatcher.terminalOwnsPaste, isFalse);
      var focused = true;
      dispatcher.terminalFocused = () => focused;
      expect(dispatcher.terminalOwnsPaste, isTrue);
      focused = false;
      expect(dispatcher.terminalOwnsPaste, isFalse);
      dispatcher.terminalFocused = null;
      expect(dispatcher.terminalOwnsPaste, isFalse);
    });

    test('a region without pictures still carries text', () {
      final log = <String>[];
      final r = region('composer', () => true, log: log);
      expect(r.picturesEnabled(), isTrue);
      r.onText('hi');
      expect(log, ['text:composer:hi']);
    });
  });

  group('decidePaste', () {
    test('an office copy (picture plus text) pastes the text into an input', () {
      final files = [png(name: 'image.png')];
      expect(decide(files: files, text: 'A1\tB1', field: true).action,
          PasteAction.field);
      expect(decide(files: files, text: 'A1\tB1', terminal: true).action,
          PasteAction.terminal);
    });

    test('a file copy offers the picture, never its name as text', () {
      final files = [png(name: 'shot.png')];
      expect(decide(files: files, text: 'shot.png', field: true).action,
          PasteAction.offer);
      expect(decide(files: files, field: true).action, PasteAction.offer);
    });

    test('outside an input a picture paste is unchanged', () {
      expect(decide(files: [png()], text: 'A1').action, PasteAction.offer);
    });

    test('text over the cap is refused', () {
      final big = 'x' * (maxPasteText + 1);
      final decision = decide(text: big, field: true);
      expect(decision.action, PasteAction.reject);
      expect(decision.reason, 'text_too_large');
      expect(decide(text: 'x' * maxPasteText, field: true).action,
          PasteAction.field);
    });

    test('text goes to the focused terminal', () {
      final decision = decide(text: 'ls -la', terminal: true);
      expect(decision.action, PasteAction.terminal);
      expect(decision.takesEvent, isTrue);
    });

    test('text goes into the focused text field when no terminal has focus', () {
      final decision = decide(text: 'hello', field: true);
      expect(decision.action, PasteAction.field);
      expect(decision.takesEvent, isTrue);
    });

    test('a terminal with focus wins over a field', () {
      expect(decide(text: 'x', terminal: true, field: true).action,
          PasteAction.terminal);
    });

    test('text nobody of ours owns keeps the browser default', () {
      final decision = decide(text: 'hello');
      expect(decision.action, PasteAction.ignore);
      expect(decision.takesEvent, isFalse);
    });

    test('a picture is offered and takes the event', () {
      final decision = decide(files: [png()], field: true);
      expect(decision.action, PasteAction.offer);
      expect(decision.takesEvent, isTrue);
    });

    test('a picture with text is the text inside an input', () {
      expect(decide(files: [png()], text: 'caption', terminal: true).action,
          PasteAction.terminal);
      // With pictures off the caller passes no files, so the text is pasted.
      expect(decide(text: 'caption', terminal: true).action,
          PasteAction.terminal);
    });

    test('a paste without text is refused with a reason when an input owns it',
        () {
      for (final owner in [
        decide(terminal: true),
        decide(field: true),
      ]) {
        expect(owner.action, PasteAction.reject);
        expect(owner.reason, 'no_text');
        expect(owner.takesEvent, isTrue);
      }
      expect(decide().action, PasteAction.ignore);
    });

    test('picture refusals keep their reasons and a busy slot refuses', () {
      expect(decide(files: [png()], busy: true).reason, 'busy');
      expect(decide(files: [png(name: 'a.png'), png(name: 'b.png', size: 2)])
          .reason, 'multiple');
      expect(decide(files: [png(size: 0)]).reason, 'empty');
      expect(decide(files: [png(size: kMaxPicturePasteBytes + 1)]).reason,
          'too_large');
      expect(
          decide(files: [const PasteCandidate(mime: 'image/gif', size: 3)])
              .reason,
          'unsupported');
    });

    test('every taken paste suppresses the browser paste: no double paste', () {
      for (final decision in [
        decide(text: 'a', terminal: true),
        decide(text: 'a', field: true),
        decide(files: [png()]),
        decide(terminal: true),
      ]) {
        expect(decision.takesEvent, isTrue, reason: '${decision.action}');
      }
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
