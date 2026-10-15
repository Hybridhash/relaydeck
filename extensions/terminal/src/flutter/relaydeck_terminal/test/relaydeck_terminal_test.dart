// RelayDeck terminal extension tests.
//
// Run from this package directory once the Flutter SDK is provisioned:
//   flutter pub get && flutter test
// Fixture files are shared with the Python suite (tests/fixtures/vt,
// tests/fixtures/keys) and loaded by relative path; a missing fixture
// fails loudly rather than silently testing nothing.
import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flet/flet.dart';
import 'package:flutter/foundation.dart'
    show debugDefaultTargetPlatformOverride;
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:xterm2/xterm.dart';

import '../lib/src/ack.dart';
import '../lib/src/relaydeck_terminal.dart';
import '../lib/src/snapshot.dart';
import '../lib/src/utf8_accumulator.dart';

const _fixtures = '../../../../../tests/fixtures';

/// Runs [body] with [platform] as the debug target and restores the default after,
/// inside the test body so a failing test cannot leak the override.
Future<void> _withPlatform(
    TargetPlatform platform, Future<void> Function() body) async {
  debugDefaultTargetPlatformOverride = platform;
  try {
    await body();
  } finally {
    debugDefaultTargetPlatformOverride = null;
  }
}

Future<_GateHarness> _pumpGate(WidgetTester tester,
    {required bool inputEnabled,
    int maxLines = 100,
    double width = 800,
    double height = 600,
    double fontSize = 14.0}) async {
  await tester.binding.setSurfaceSize(Size(width, height));
  addTearDown(() => tester.binding.setSurfaceSize(null));
  final factory = _FakeDataChannelFactory();
  final backend = FletBackend(
    pageUri: Uri.parse('http://localhost/'),
    assetsDir: '',
    multiView: false,
    extensions: const <FletExtension>[],
    dataChannelFactory: factory,
  );
  final control = Control(
    id: 1,
    type: 'RelayDeckTerminal',
    properties: {
      'input_enabled': inputEnabled,
      'max_lines': maxLines,
      'font_size': fontSize,
    },
    backend: backend,
  );
  await tester.pumpWidget(
    ChangeNotifierProvider<FletBackend>.value(
      value: backend,
      child: MaterialApp(
        home: Scaffold(
          body: SizedBox(
            width: width,
            height: height,
            child: RelayDeckTerminalControl(control: control),
          ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  // Tap focuses the embedded TerminalView for hardware-key delivery.
  await tester.tap(find.byType(TerminalView));
  await tester.pump();
  return _GateHarness(factory: factory, control: control);
}

List<String> _normalized(Terminal t) {
  // The vt goldens and the wire snapshot share one semantic: the
  // production snapshotLines (grid-position restoration, wrapped join,
  // per-line rstrip, trailing empties dropped). Any divergence breaks
  // the fixture suite below.
  return snapshotLines(t);
}

void _feed(Terminal t, List<int> bytes) {
  t.write(Utf8Accumulator().add(bytes));
}

void main() {
  Map<String, dynamic> jsonFixture(String name) => json.decode(
        File('$_fixtures/vt/$name').readAsStringSync(),
      ) as Map<String, dynamic>;

  group('vt fixtures', () {
    for (final stem in [
      '01_ascii_colors',
      '02_cursor_addressing',
      '03_erased_line',
      '05_wide_emoji',
      '06_combining',
    ]) {
      test(stem, () {
        final bytes = File('$_fixtures/vt/$stem.bin').readAsBytesSync();
        final expected =
            (jsonFixture('$stem.json')['lines'] as List).cast<String>();
        final t = Terminal(maxLines: 100);
        _feed(t, bytes);
        expect(_normalized(t), expected);
      });
    }

    test('04 alternate screen preserves main buffer', () {
      final spec = jsonFixture('04_alt_screen.json');
      final parts = (spec['parts'] as List).cast<String>();
      final t = Terminal(maxLines: 100);
      _feed(t, File('$_fixtures/vt/${parts[0]}').readAsBytesSync());
      _feed(t, File('$_fixtures/vt/${parts[1]}').readAsBytesSync());
      expect(t.isUsingAltBuffer, isTrue);
      expect(
        _normalized(t),
        (spec['lines_during_alt'] as List).cast<String>(),
      );
      _feed(t, File('$_fixtures/vt/${parts[2]}').readAsBytesSync());
      expect(t.isUsingAltBuffer, isFalse);
      expect(
        _normalized(t),
        (spec['lines_after_exit'] as List).cast<String>(),
      );
    });

    test('07 bracketed paste mode toggles', () {
      final spec = jsonFixture('07_bracketed_mode.json');
      final t = Terminal(maxLines: 100);
      _feed(
        t,
        File('$_fixtures/vt/${spec['on_part']}').readAsBytesSync(),
      );
      expect(t.bracketedPasteMode, isTrue);
      _feed(
        t,
        File('$_fixtures/vt/${spec['off_part']}').readAsBytesSync(),
      );
      expect(t.bracketedPasteMode, isFalse);
    });

    test('08 scrollback keeps visible suffix', () {
      final spec = jsonFixture('08_scrollback.json');
      final bytes = File('$_fixtures/vt/08_scrollback.bin').readAsBytesSync();
      final t = Terminal(maxLines: 100);
      _feed(t, bytes);
      final got = _normalized(t);
      final want = (spec['suffix_lines'] as List).cast<String>();
      expect(got.length, greaterThanOrEqualTo(want.length));
      expect(got.sublist(got.length - want.length), want);
    });

    test('09 split delivery decodes identically at every cut', () {
      final spec = jsonFixture('09_split_sequences.json');
      final bytes =
          File('$_fixtures/vt/09_split_sequences.bin').readAsBytesSync();
      final want = spec['decoded_text'] as String;
      for (var cut = 0; cut <= bytes.length; cut++) {
        final acc = Utf8Accumulator();
        final text =
            acc.add(bytes.sublist(0, cut)) + acc.add(bytes.sublist(cut));
        expect(text, want, reason: 'cut at $cut');
      }
    });
  });

  group('key encodings', () {
    TerminalKey keyFor(String name) => switch (name) {
          'escape' => TerminalKey.escape,
          'tab' => TerminalKey.tab,
          'enter' => TerminalKey.enter,
          'backspace' => TerminalKey.backspace,
          'arrowLeft' => TerminalKey.arrowLeft,
          'arrowUp' => TerminalKey.arrowUp,
          'arrowDown' => TerminalKey.arrowDown,
          'arrowRight' => TerminalKey.arrowRight,
          'home' => TerminalKey.home,
          'end' => TerminalKey.end,
          'insert' => TerminalKey.insert,
          'delete' => TerminalKey.delete,
          'f1' => TerminalKey.f1,
          'f2' => TerminalKey.f2,
          'f3' => TerminalKey.f3,
          'f4' => TerminalKey.f4,
          'f5' => TerminalKey.f5,
          'f6' => TerminalKey.f6,
          'f7' => TerminalKey.f7,
          'f8' => TerminalKey.f8,
          'f9' => TerminalKey.f9,
          'f10' => TerminalKey.f10,
          'f11' => TerminalKey.f11,
          'f12' => TerminalKey.f12,
          'pageUp' => TerminalKey.pageUp,
          'pageDown' => TerminalKey.pageDown,
          'keyC' => TerminalKey.keyC,
          'keyD' => TerminalKey.keyD,
          'keyZ' => TerminalKey.keyZ,
          _ => throw ArgumentError('unknown key $name'),
        };

    test('xterm2 defaults match the shared golden table', () {
      final spec = json.decode(
        File('$_fixtures/keys/encodings.json').readAsStringSync(),
      ) as Map<String, dynamic>;
      for (final entry in (spec['encodings'] as List).cast<Map>()) {
        if (entry['source'] == 'shift-enter-override') continue;
        final t = Terminal(maxLines: 10);
        final out = StringBuffer();
        t.onOutput = out.write;
        final mods = (entry['mods'] as List).cast<String>();
        t.keyInput(
          keyFor(entry['key'] as String),
          shift: mods.contains('shift'),
          alt: mods.contains('alt'),
          ctrl: mods.contains('ctrl'),
        );
        final wantBytes = [
          for (var i = 0; i < (entry['bytes_hex'] as String).length; i += 2)
            int.parse((entry['bytes_hex'] as String).substring(i, i + 2),
                radix: 16),
        ];
        expect(
          utf8.encode(out.toString()),
          wantBytes,
          reason: entry['name'] as String,
        );
      }
    });

    test('shift-enter default diverges (why the widget intercepts)', () {
      // Locks the xterm2 5.2.0 defaults so the Shift+Enter intercept
      // (Shift+Enter -> 0x0A in onKeyEvent) stays justified. Physical
      // Enter arrives as TerminalKey.enter, whose keytab rules have no
      // shift variant, so shifted Enter falls through to Enter-NewLine
      // (CR) — NOT to the ESC O M rule, which names Qt Return =
      // TerminalKey.returnKey (unreachable from the Flutter key path).
      // If a future xterm2 changes either default, revisit the intercept.
      final t = Terminal(maxLines: 10);
      final out = StringBuffer();
      t.onOutput = out.write;
      t.keyInput(TerminalKey.enter, shift: true);
      expect(utf8.encode(out.toString()), [0x0D]); // CR
      out.clear();
      t.keyInput(TerminalKey.returnKey, shift: true);
      expect(utf8.encode(out.toString()), [0x1B, 0x4F, 0x4D]); // ESC O M
    });
  });

  group('selection', () {
    test('anchored range extracts exact text', () {
      final t = Terminal(maxLines: 100);
      final controller = TerminalController();
      _feed(t, utf8.encode('hello world\r\nsecond line'));
      controller.setSelection(
        t.buffer.createAnchor(0, 0),
        t.buffer.createAnchor(11, 0),
      );
      final selection = controller.selectionFor(t.buffer);
      expect(selection, isNotNull);
      expect(t.buffer.getText(selection, true), 'hello world');
    });
  });

  group('readOnly behavior (library lock, xterm2 5.2.0)', () {
    // Library truth: TerminalView._handleKeyEvent never consults readOnly,
    // so hardware-key bytes reach onOutput in BOTH modes (locked here so a
    // future xterm2 behavior change is caught loudly). The Composer-mode
    // display-only gate therefore lives in RelayDeckTerminalState
    // (_onTerminalOutput drops engine output while input is disabled) and
    // is locked by the widget tests below.
    Future<int> pumpAndType(WidgetTester tester,
        {required bool readOnly}) async {
      final t = Terminal(maxLines: 10);
      var calls = 0;
      t.onOutput = (_) => calls++;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 800,
              height: 600,
              child: TerminalView(
                t,
                readOnly: readOnly,
                autofocus: true,
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
      await tester.pump();
      return calls;
    }

    testWidgets('key bytes reach onOutput regardless of readOnly',
        (tester) async {
      expect(await pumpAndType(tester, readOnly: true), 1);
      expect(await pumpAndType(tester, readOnly: false), 1);
    });
  });

  group('widget input gate (display-only mode)', () {
    // The widget — not xterm2's readOnly — is the Composer display-only
    // gate: while input_enabled is false, engine output must never reach
    // the data channel as TERMINAL_INPUT (0x81), but selection and the
    // INTERCEPT path (shift-enter -> 0x0A, widgets-only) still work once
    // input is re-enabled over INPUT_ENABLED (0x03).
    testWidgets('disabled input drops key bytes from the channel',
        (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: false);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
      await tester.pump();
      expect(harness.inputs(), isEmpty,
          reason: 'Composer display-only mode must not forward keys');
    });

    testWidgets(
        'INPUT_ENABLED packet re-arms key forwarding',
        (tester) => _withPlatform(TargetPlatform.macOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: false);
              harness.factory.channel!.incoming
                  .add(Uint8List.fromList([0x03, 0x01]));
              await tester.pump();
              await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
              await tester.pump();
              final inputs = harness.inputs();
              expect(inputs, hasLength(1));
              expect(inputs.single.sublist(6), [0x61]); // 'a' payload
            }));

    testWidgets(
        'shift-enter emits 0x0A once input is enabled',
        (tester) => _withPlatform(TargetPlatform.macOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              await tester.sendKeyDownEvent(LogicalKeyboardKey.shift);
              await tester.sendKeyEvent(LogicalKeyboardKey.enter);
              await tester.sendKeyUpEvent(LogicalKeyboardKey.shift);
              await tester.pump();
              final inputs = harness.inputs();
              expect(inputs, hasLength(1));
              expect(inputs.single.sublist(6), [0x0A]);
            }));
  });

  group('touch focus gated until armed', () {
    FocusNode focusOf(WidgetTester tester) =>
        tester.widget<TerminalView>(find.byType(TerminalView)).focusNode!;

    testWidgets(
        'touch: a tap does not focus or open the keyboard until armed',
        (tester) => _withPlatform(TargetPlatform.iOS, () async {
              await _pumpGate(tester, inputEnabled: true);
              expect(focusOf(tester).canRequestFocus, isFalse);
              await tester.tap(find.byType(TerminalView));
              await tester.pump();
              await tester.pump();
              expect(focusOf(tester).hasFocus, isFalse);
              expect(tester.testTextInput.isVisible, isFalse);
              expect(
                  tester
                      .widget<TerminalView>(find.byType(TerminalView))
                      .readOnly,
                  isFalse,
                  reason: 'the text input stays mounted');
            }));

    testWidgets(
        'REQUEST_KEYBOARD arms focus and opens the keyboard',
        (tester) => _withPlatform(TargetPlatform.iOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              harness.factory.channel!.incoming.add(Uint8List.fromList([0x0A]));
              await tester.pump();
              await tester.pump();
              expect(focusOf(tester).canRequestFocus, isTrue);
              expect(tester.testTextInput.isVisible, isTrue);
              expect(harness.inputs(), isEmpty,
                  reason: 'never writes to the PTY');
            }));

    testWidgets(
        'RELEASE_KEYBOARD hides the keyboard and disarms focus',
        (tester) => _withPlatform(TargetPlatform.iOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              harness.factory.channel!.incoming.add(Uint8List.fromList([0x0A]));
              await tester.pump();
              await tester.pump();
              harness.factory.channel!.incoming.add(Uint8List.fromList([0x0E]));
              await tester.pump();
              await tester.pump();
              expect(tester.testTextInput.isVisible, isFalse);
              expect(focusOf(tester).canRequestFocus, isFalse);
              expect(harness.inputs(), isEmpty,
                  reason: 'never writes to the PTY');
            }));

    testWidgets(
        'losing focus disarms touch focus again',
        (tester) => _withPlatform(TargetPlatform.iOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              harness.factory.channel!.incoming.add(Uint8List.fromList([0x0A]));
              await tester.pump();
              await tester.pump();
              focusOf(tester).unfocus();
              await tester.pump();
              expect(focusOf(tester).canRequestFocus, isFalse);
            }));

    testWidgets(
        'desktop focus is never gated',
        (tester) => _withPlatform(TargetPlatform.macOS, () async {
              await _pumpGate(tester, inputEnabled: true);
              expect(focusOf(tester).canRequestFocus, isTrue);
            }));
  });

  group('native keyboard button', () {
    final button = find.byKey(const ValueKey('keyboard-button'));
    FocusNode focusOf(WidgetTester tester) =>
        tester.widget<TerminalView>(find.byType(TerminalView)).focusNode!;

    testWidgets(
        'the button shows only when Python enables it and input is enabled',
        (tester) => _withPlatform(TargetPlatform.iOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              expect(button, findsNothing);
              harness.factory.channel!.incoming
                  .add(Uint8List.fromList([0x0F, 0x01]));
              await tester.pump();
              await tester.pump();
              expect(button, findsOneWidget);
            }));

    testWidgets(
        'the button stays hidden while input is disabled',
        (tester) => _withPlatform(TargetPlatform.iOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: false);
              harness.factory.channel!.incoming
                  .add(Uint8List.fromList([0x0F, 0x01]));
              await tester.pump();
              await tester.pump();
              expect(button, findsNothing);
            }));

    testWidgets(
        'a tap focuses synchronously, opens the keyboard and reports shown',
        (tester) => _withPlatform(TargetPlatform.iOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              harness.factory.channel!.incoming
                  .add(Uint8List.fromList([0x0F, 0x01]));
              await tester.pump();
              await tester.pump();
              await tester.tap(button);
              expect(focusOf(tester).hasFocus, isTrue);
              expect(tester.testTextInput.isVisible, isTrue);
              expect(harness.keyboardStates().last, [0x88, 0x01]);
              expect(harness.inputs(), isEmpty,
                  reason: 'never writes to the PTY');
            }));

    testWidgets(
        'a second tap hides the keyboard and reports hidden',
        (tester) => _withPlatform(TargetPlatform.iOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              harness.factory.channel!.incoming
                  .add(Uint8List.fromList([0x0F, 0x01]));
              await tester.pump();
              await tester.pump();
              await tester.tap(button);
              await tester.tap(button);
              await tester.pump();
              expect(focusOf(tester).hasFocus, isFalse);
              expect(focusOf(tester).canRequestFocus, isFalse);
              expect(tester.testTextInput.isVisible, isFalse);
              expect(harness.keyboardStates().last, [0x88, 0x00]);
            }));

    testWidgets(
        'a hide tap survives focus loss between pointer down and up',
        (tester) => _withPlatform(TargetPlatform.iOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              harness.factory.channel!.incoming
                  .add(Uint8List.fromList([0x0F, 0x01]));
              await tester.pump();
              await tester.pump();
              await tester.tap(button);
              await tester.pump();
              expect(focusOf(tester).hasFocus, isTrue);
              final before = harness.keyboardStates().length;
              final gesture =
                  await tester.startGesture(tester.getCenter(button));
              focusOf(tester).unfocus();
              await tester.pump();
              await gesture.up();
              await tester.pump();
              expect(focusOf(tester).hasFocus, isFalse);
              expect(focusOf(tester).canRequestFocus, isFalse);
              expect(tester.testTextInput.isVisible, isFalse);
              expect(
                  find.byWidgetPredicate((widget) =>
                      widget is Semantics &&
                      widget.properties.label == 'Show keyboard'),
                  findsOneWidget);
              final states = harness.keyboardStates().skip(before).toList();
              expect(states, isNotEmpty);
              expect(states.every((state) => state[1] == 0), isTrue,
                  reason:
                      'a hide gesture must never rearm after incidental focus loss');
              expect(harness.inputs(), isEmpty);
            }));

    for (final loseFocus in [false, true]) {
      testWidgets(
          'canceling a keyboard tap reconciles focus: $loseFocus',
          (tester) => _withPlatform(TargetPlatform.iOS, () async {
                final harness = await _pumpGate(tester, inputEnabled: true);
                harness.factory.channel!.incoming
                    .add(Uint8List.fromList([0x0F, 0x01]));
                await tester.pump();
                await tester.pump();
                await tester.tap(button);
                await tester.pump();
                final gesture =
                    await tester.startGesture(tester.getCenter(button));
                if (loseFocus) {
                  focusOf(tester).unfocus();
                  await tester.pump();
                }
                await gesture.moveBy(const Offset(100, 0));
                await gesture.up();
                await tester.pump();
                expect(focusOf(tester).canRequestFocus, !loseFocus);
                expect(tester.testTextInput.isVisible, !loseFocus);
                expect(harness.keyboardStates().last[1], loseFocus ? 0 : 1);
                expect(
                    harness.factory.channel!.sent.any((packet) =>
                        packet.length == 5 &&
                        packet[0] == 0x89 &&
                        packet[1] == 3),
                    isTrue);
                expect(harness.inputs(), isEmpty);
              }));
    }

    testWidgets(
        'touch keyboard traces encode gesture flags and delayed samples',
        (tester) => _withPlatform(TargetPlatform.iOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              List<Uint8List> traces(int event) => harness.factory.channel!.sent
                  .where((p) => p.length == 5 && p[0] == 0x89 && p[1] == event)
                  .toList();
              harness.factory.channel!.incoming
                  .add(Uint8List.fromList([0x0F, 0x01]));
              await tester.pump();
              await tester.pump();
              final show = await tester.startGesture(tester.getCenter(button));
              expect(traces(1).last[2], 8);
              await show.up();
              await tester.pump();
              expect(traces(2).last[2] & 0x19, 9);
              await tester.pump(const Duration(milliseconds: 400));
              expect(traces(9).last[2], 15);
              final hide = await tester.startGesture(tester.getCenter(button));
              expect(traces(1).last[2], 31);
              await hide.up();
              await tester.pump();
              expect(traces(2).last[2] & 0x19, 24);
              await tester.pump(const Duration(milliseconds: 400));
              expect(traces(9).last[2], 24);
              expect(traces(4), isNotEmpty);
              expect(traces(5), isNotEmpty);
              for (final event in [1, 2, 4, 5, 9]) {
                for (final packet in traces(event)) {
                  expect(ByteData.sublistView(packet).getUint16(3, Endian.big),
                      0xFFFF);
                }
              }
              expect(harness.inputs(), isEmpty);
            }));

    testWidgets(
        'touch keyboard traces include command samples and immediate disable',
        (tester) => _withPlatform(TargetPlatform.iOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              harness.factory.channel!.incoming
                  .add(Uint8List.fromList([0x0F, 0x01, 0x0A]));
              await tester.pump();
              await tester.pump();
              await tester.pump(const Duration(milliseconds: 400));
              final samplesBeforeRelease = harness.factory.channel!.sent
                  .where((p) => p.length == 5 && p[0] == 0x89 && p[1] == 9)
                  .length;
              expect(samplesBeforeRelease, 1);
              final gesture =
                  await tester.startGesture(tester.getCenter(button));
              harness.factory.channel!.incoming
                  .add(Uint8List.fromList([0x03, 0x00]));
              await tester.pump();
              expect(focusOf(tester).canRequestFocus, isFalse);
              expect(harness.keyboardStates().last[1], 0);
              await gesture.cancel();
              harness.factory.channel!.incoming.add(Uint8List.fromList([0x0E]));
              await tester.pump();
              await tester.pump(const Duration(milliseconds: 400));
              final packets = harness.factory.channel!.sent
                  .where((p) => p.length == 5 && p[0] == 0x89)
                  .toList();
              expect(packets.any((p) => p[1] == 6 && (p[2] & 9) == 9), isTrue);
              expect(packets.any((p) => p[1] == 8 && (p[2] & 9) == 0), isTrue);
              expect(packets.any((p) => p[1] == 7 && (p[2] & 1) == 0), isTrue);
              expect(packets.where((p) => p[1] == 9).length,
                  samplesBeforeRelease + 1);
              expect(harness.inputs(), isEmpty);
            }));

    testWidgets(
        'desktop keyboard events emit no touch trace',
        (tester) => _withPlatform(TargetPlatform.macOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              harness.factory.channel!.incoming
                  .add(Uint8List.fromList([0x0F, 0x01]));
              await tester.pump();
              await tester.pump();
              await tester.tap(button);
              await tester.tap(button);
              harness.factory.channel!.incoming
                  .add(Uint8List.fromList([0x0A, 0x0E, 0x03, 0x00]));
              await tester.pump(const Duration(milliseconds: 400));
              expect(
                  harness.factory.channel!.sent
                      .where((p) => p.isNotEmpty && p[0] == 0x89),
                  isEmpty);
            }));
  });

  group('REQUEST_KEYBOARD (0x0A, Direct Input icon)', () {
    testWidgets('opens the soft keyboard while input is enabled',
        (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      // The harness may already show it; start from a dismissed keyboard.
      tester.testTextInput.hide();
      await tester.pump();
      expect(tester.testTextInput.isVisible, isFalse);
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x0A]));
      await tester.pump();
      await tester.pump();
      expect(tester.testTextInput.isVisible, isTrue);
      expect(harness.inputs(), isEmpty, reason: 'never writes to the PTY');
    });

    testWidgets('ignored while input is disabled', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: false);
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x0A]));
      await tester.pump();
      await tester.pump();
      expect(tester.testTextInput.isVisible, isFalse);
      expect(harness.inputs(), isEmpty);
    });

    testWidgets('disable before the post-frame keeps the keyboard closed',
        (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      // One chunk: request, then INPUT_ENABLED off; the next packet still
      // parses after the 1-byte request.
      harness.factory.channel!.incoming
          .add(Uint8List.fromList([0x0A, 0x03, 0x00]));
      await tester.pump();
      await tester.pump();
      expect(tester.testTextInput.isVisible, isFalse);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
      await tester.pump();
      expect(harness.inputs(), isEmpty, reason: 'disable packet applied');
    });

    testWidgets('request after dispose is harmless', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      final channel = harness.factory.channel!;
      await tester.pumpWidget(const SizedBox());
      // Dispose closes the channel; a late request has nowhere to land.
      if (!channel.closed) channel.incoming.add(Uint8List.fromList([0x0A]));
      await tester.pump();
      await tester.pump();
      expect(tester.takeException(), isNull);
      expect(tester.testTextInput.isVisible, isFalse);
    });
  });

  group('engine snapshot (0x09/0x84)', () {
    // The Select Text sheet source: emulator truth (scrollback +
    // screen, overwrites applied, escapes consumed), frozen per
    // request and tagged by request id. Fed through real WRITE
    // packets so framing, incremental UTF-8, and engine state are
    // all in the loop.
    Future<void> feed(
        WidgetTester tester, _GateHarness harness, String data) async {
      final bytes = utf8.encode(data);
      final packet = BytesBuilder(copy: false)
        ..addByte(0x01)
        ..add(_u32be(bytes.length))
        ..add(bytes);
      harness.factory.channel!.incoming.add(packet.toBytes());
      await tester.pump();
    }

    Future<void> request(
        WidgetTester tester, _GateHarness harness, int reqId) async {
      final packet = BytesBuilder(copy: false)
        ..addByte(0x09)
        ..add(_u32be(reqId));
      harness.factory.channel!.incoming.add(packet.toBytes());
      await tester.pump();
    }

    testWidgets('sgr and cursor overwrite resolve to emulator truth',
        (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await feed(tester, harness, '\x1b[31mred\x1b[0m\rX');
      await request(tester, harness, 1);
      expect(_snapshots(harness)[1], 'Xed');
    });

    testWidgets('scrollback included beyond the visible screen',
        (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      final lines =
          List<String>.generate(60, (i) => 'l${i.toString().padLeft(2, '0')}');
      await feed(tester, harness, '${lines.join('\r\n')}\r\n');
      await request(tester, harness, 1);
      final got = _snapshots(harness)[1]!.split('\n');
      expect(got, hasLength(60));
      expect(got.first, 'l00');
      expect(got.last, 'l59');
    });

    testWidgets('large buffer splits into reassemblable chunks',
        (tester) async {
      final harness =
          await _pumpGate(tester, inputEnabled: true, maxLines: 5000);
      final line = 'x' * 59;
      final lines = List<String>.generate(2000, (i) => '$i:$line');
      await feed(tester, harness, '${lines.join('\r\n')}\r\n');
      await request(tester, harness, 1);
      final chunks =
          _snapshotChunks(harness).where((c) => c.reqId == 1).toList();
      expect(chunks.length, greaterThan(1));
      expect(_snapshots(harness)[1], lines.join('\n'));
    });

    testWidgets('concurrent requests both complete tagged by id',
        (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await feed(tester, harness, 'hello');
      await request(tester, harness, 5);
      await request(tester, harness, 6);
      final snaps = _snapshots(harness);
      expect(snaps[5], 'hello');
      expect(snaps[6], 'hello');
    });

    testWidgets('alt screen snapshots alt content only', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await feed(tester, harness, 'MAIN\r\n');
      await feed(tester, harness, '\x1b[?1049hALT');
      await request(tester, harness, 1);
      expect(_snapshots(harness)[1], 'ALT');
      await feed(tester, harness, '\x1b[?1049l');
      await request(tester, harness, 2);
      expect(_snapshots(harness)[2], 'MAIN');
    });

    testWidgets('cursor-addressed leading position preserved', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await feed(tester, harness, '\x1b[1;10HX');
      await request(tester, harness, 1);
      expect(_snapshots(harness)[1], '         X');
    });

    testWidgets('soft-wrapped long line joins without newline', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      final long = 'w' * 300;
      await feed(tester, harness, long);
      await request(tester, harness, 1);
      expect(_snapshots(harness)[1], long);
    });

    testWidgets('blank screen completes with one empty chunk', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await request(tester, harness, 1);
      expect(_snapshots(harness)[1], '');
      expect(_snapshotChunks(harness).where((c) => c.reqId == 1), hasLength(1));
    });

    testWidgets('maxed scrollback stays inside the protocol budget',
        (tester) async {
      // protocol.py MAX_SNAPSHOT_TOTAL is 4 MiB: a full 5000-row
      // scrollback (~250 KB over 8 chunks) must pass, proving the
      // realistic maximum fits the documented budget. Rows stay
      // under the view width so nothing wraps (wrapped rows would
      // multiply physical lines past maxLines and trim the head).
      final harness =
          await _pumpGate(tester, inputEnabled: true, maxLines: 5000);
      final line = 'y' * 44;
      final lines = List<String>.generate(
          4999, (i) => '${i.toString().padLeft(4, '0')}:$line');
      await feed(tester, harness, '${lines.join('\r\n')}\r\n');
      await request(tester, harness, 1);
      final chunks =
          _snapshotChunks(harness).where((c) => c.reqId == 1).toList();
      expect(chunks, isNotEmpty);
      expect(chunks.first.total, lessThan(4 * 1024 * 1024));
      expect(_snapshots(harness)[1], lines.join('\n'));
    });
  });

  group('ack encoding (0xFF dart2js)', () {
    // ByteData.setUint64 throws on dart2js, so the encoder writes two
    // supported u32be halves to keep delivery progressing. These vectors pin
    // the exact bytes (also executed compiled to JS via tool/ack_js).
    List<int> body(int applied) {
      final p = encodeAck(applied);
      expect(p[0], 0xFF);
      expect(p.length, 9);
      return p.sublist(1);
    }

    test('zero and small counters encode big-endian', () {
      expect(body(0), [0, 0, 0, 0, 0, 0, 0, 0]);
      expect(body(52), [0, 0, 0, 0, 0, 0, 0, 52]);
    });

    test('u32 boundary splits into halves', () {
      expect(body(0xFFFFFFFF), [0, 0, 0, 0, 0xFF, 0xFF, 0xFF, 0xFF]);
      expect(body(0x100000000), [0, 0, 0, 1, 0, 0, 0, 0]);
      expect(body(0x100000001), [0, 0, 0, 1, 0, 0, 0, 1]);
    });

    test('2^53 - 1 is exact, beyond saturates at the bound', () {
      // Under-ACK, never over-ACK: bytes past the bound are NOT
      // acknowledged (backpressure stays conservative; a reset
      // zeroes both sides if the unreachable bound is ever hit).
      final bound = [0x00, 0x1F, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF];
      expect(body(9007199254740991), bound);
      expect(body(9007199254740992), bound);
    });

    test('negative clamps to zero', () {
      expect(body(-1), [0, 0, 0, 0, 0, 0, 0, 0]);
    });
  });

  group('ack flow (0xFF)', () {
    // The encoder from ack.dart, pinned end to end through the widget:
    // cumulative applied bytes after writes, zeroed by reset. (There
    // is deliberately no catch-and-drop for malformed packets: dropping
    // bytes and resuming as live would silently lose terminal output.)
    Future<void> feed(
        WidgetTester tester, _GateHarness harness, String data) async {
      final bytes = utf8.encode(data);
      harness.factory.channel!.incoming.add(
        (BytesBuilder(copy: false)
              ..addByte(0x01)
              ..add(_u32be(bytes.length))
              ..add(bytes))
            .toBytes(),
      );
      await tester.pump();
    }

    testWidgets('writes ack cumulative applied bytes', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await feed(tester, harness, 'hi');
      await feed(tester, harness, 'bye');
      expect(harness.acks(), [2, 5]);
    });

    testWidgets('reset zeroes the ack counter', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await feed(tester, harness, 'hi');
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x08]));
      await tester.pump();
      expect(harness.acks(), [2, 0]);
    });
  });

  group('engine mode report (0x85)', () {
    // Paste framing follows the REAL xterm2 mode, reported after every
    // WRITE and RESET — never a Python-side re-parse of output bytes
    // (which provably diverges across RIS and split param lists).
    Future<void> feed(
        WidgetTester tester, _GateHarness harness, String data) async {
      final bytes = utf8.encode(data);
      final packet = BytesBuilder(copy: false)
        ..addByte(0x01)
        ..add(_u32be(bytes.length))
        ..add(bytes);
      harness.factory.channel!.incoming.add(packet.toBytes());
      await tester.pump();
    }

    Future<void> reset(WidgetTester tester, _GateHarness harness) async {
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x08]));
      await tester.pump();
    }

    testWidgets('decset 2004 write reports bracketed on', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await feed(tester, harness, '\x1b[?2004hready \$ ');
      expect(harness.modes().last, isTrue);
    });

    testWidgets('decset then decrst reports bracketed off', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await feed(tester, harness, '\x1b[?2004hready \$ ');
      expect(harness.modes().last, isTrue);
      await feed(tester, harness, '\x1b[?2004l\$ ');
      expect(harness.modes().last, isFalse);
    });

    testWidgets('ris after decset reports off (regex blind spot)',
        (tester) async {
      // The rejected regex stayed enabled here (RIS is not a 2004
      // reset sequence); the engine really leaves bracketed mode.
      final harness = await _pumpGate(tester, inputEnabled: true);
      await feed(tester, harness, '\x1b[?2004hready \$ ');
      expect(harness.modes().last, isTrue);
      await feed(tester, harness, '\x1bc');
      expect(harness.modes().last, isFalse);
    });

    testWidgets('reset packet reports bracketed off', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await feed(tester, harness, '\x1b[?2004hready \$ ');
      expect(harness.modes().last, isTrue);
      await reset(tester, harness);
      expect(harness.modes().last, isFalse);
    });

    testWidgets('plain writes report the standing mode', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await feed(tester, harness, 'hello');
      expect(harness.modes().last, isFalse);
      await feed(tester, harness, '\x1b[?2004h');
      await feed(tester, harness, 'more output');
      expect(harness.modes().last, isTrue);
    });
  });

  group('SCROLL_VIEWPORT / VIEW_REPORT (0x0B/0x86)', () {
    // Local view scrolling for the page up/down buttons: jumps move the
    // TerminalView ScrollController only, never the emulator or PTY,
    // and Dart reports (alt, at-bottom, mouse) back on change only.
    Future<void> feed(
        WidgetTester tester, _GateHarness harness, String data) async {
      final bytes = utf8.encode(data);
      final packet = BytesBuilder(copy: false)
        ..addByte(0x01)
        ..add(_u32be(bytes.length))
        ..add(bytes);
      harness.factory.channel!.incoming.add(packet.toBytes());
      await tester.pump();
    }

    Future<void> scroll(
        WidgetTester tester, _GateHarness harness, int body) async {
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x0B, body]));
      await tester.pump();
      await tester.pump();
    }

    double pixels(WidgetTester tester) {
      // The TerminalView scrollable with real extent (the hidden text
      // field also owns a Scrollable, but with zero extent).
      final states = tester.stateList<ScrollableState>(
        find.descendant(
          of: find.byType(TerminalView),
          matching: find.byType(Scrollable),
        ),
      );
      final withExtent =
          states.where((s) => s.position.maxScrollExtent > 0).toList();
      expect(withExtent, hasLength(1));
      return withExtent.single.position.pixels;
    }

    Future<_GateHarness> pumpTall(WidgetTester tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      final lines = List<String>.generate(
          100, (i) => 'row${i.toString().padLeft(3, '0')}');
      await feed(tester, harness, '${lines.join('\r\n')}\r\n');
      await tester.pump();
      return harness;
    }

    testWidgets('page up moves offset and reports not at bottom',
        (tester) async {
      final harness = await pumpTall(tester);
      await scroll(tester, harness, 0x7F); // bottom first
      final before = pixels(tester);
      expect(before, greaterThan(0));
      expect(harness.views().last & 0x02, 0x02);
      await scroll(tester, harness, 0xFF); // -1 page
      expect(pixels(tester), lessThan(before));
      expect(harness.views().last & 0x02, 0x00);
      expect(harness.inputs(), isEmpty, reason: 'never writes to the PTY');
    });

    testWidgets('jump to bottom reports at bottom', (tester) async {
      final harness = await pumpTall(tester);
      await scroll(tester, harness, 0x7F);
      expect(harness.views().last & 0x02, 0x02);
      final states = tester.stateList<ScrollableState>(
        find.descendant(
          of: find.byType(TerminalView),
          matching: find.byType(Scrollable),
        ),
      );
      final withExtent =
          states.where((s) => s.position.maxScrollExtent > 0).single;
      expect(withExtent.position.pixels, withExtent.position.maxScrollExtent);
      expect(harness.inputs(), isEmpty);
    });

    bool atTail(WidgetTester tester) {
      final states = tester.stateList<ScrollableState>(
        find.descendant(
          of: find.byType(TerminalView),
          matching: find.byType(Scrollable),
        ),
      );
      final real =
          states.where((s) => s.position.maxScrollExtent > 0).toList();
      if (real.isEmpty) return true;
      final p = real.single.position;
      return p.maxScrollExtent - p.pixels < 1.0;
    }

    testWidgets('at the bottom, new output keeps the view at the bottom',
        (tester) async {
      final harness = await pumpTall(tester);
      await scroll(tester, harness, 0x7F);
      expect(atTail(tester), isTrue);
      for (var i = 0; i < 5; i++) {
        await feed(tester, harness, 'more output $i\r\n' * 20);
        await tester.pump();
        expect(atTail(tester), isTrue, reason: 'after write $i');
      }
    });

    testWidgets('a viewport change while output arrives keeps the tail',
        (tester) async {
      final harness = await pumpTall(tester);
      await scroll(tester, harness, 0x7F);
      // The soft keyboard opens (the viewport shrinks) and output arrives together.
      await tester.binding.setSurfaceSize(const Size(800, 300));
      await feed(tester, harness, 'while resizing\r\n' * 10);
      await tester.pumpAndSettle();
      expect(atTail(tester), isTrue);
      await tester.binding.setSurfaceSize(const Size(800, 600));
      await feed(tester, harness, 'after resize\r\n' * 10);
      await tester.pumpAndSettle();
      expect(atTail(tester), isTrue);
    });

    testWidgets('a reader who scrolled up is not pulled back by output',
        (tester) async {
      final harness = await pumpTall(tester);
      await scroll(tester, harness, 0x7F);
      await scroll(tester, harness, 0xFF); // one page up
      expect(atTail(tester), isFalse);
      await feed(tester, harness, 'new output\r\n' * 20);
      await tester.pumpAndSettle();
      expect(atTail(tester), isFalse);
      expect(harness.views().last & 0x02, 0x00);
      await scroll(tester, harness, 0x7F); // jump to latest re-pins
      await feed(tester, harness, 'again\r\n' * 5);
      await tester.pumpAndSettle();
      expect(atTail(tester), isTrue);
    });

    testWidgets('alt buffer reports alt and scroll is a no-op', (tester) async {
      final harness = await pumpTall(tester);
      await feed(tester, harness, '\x1b[?1049hALT');
      await tester.pump();
      expect(harness.views().last & 0x01, 0x01);
      final reports = harness.views().length;
      // Scroll plus INPUT_ENABLED off in one chunk: the 2-byte scroll
      // consumes exactly its bytes and the disable still parses.
      harness.factory.channel!.incoming
          .add(Uint8List.fromList([0x0B, 0xFF, 0x03, 0x00]));
      await tester.pump();
      await tester.pump();
      expect(harness.views().length, reports,
          reason: 'alt-buffer scroll sends no new report');
      await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
      await tester.pump();
      expect(harness.inputs(), isEmpty,
          reason: 'disable packet after the scroll was applied');
    });

    testWidgets('mouse mode sets the mouse bit', (tester) async {
      final harness = await pumpTall(tester);
      await feed(tester, harness, '\x1b[?1000h');
      await tester.pump();
      expect(harness.views().last & 0x04, 0x04);
      expect(harness.views().last & 0x01, 0x00);
      await feed(tester, harness, '\x1b[?1000l');
      await tester.pump();
      expect(harness.views().last & 0x04, 0x00);
    });

    testWidgets('no report sent when nothing changed', (tester) async {
      final harness = await pumpTall(tester);
      await scroll(tester, harness, 0x7F);
      final reports = harness.views().length;
      expect(reports, greaterThan(0));
      await tester.pump();
      await tester.pump();
      expect(harness.views().length, reports);
    });

    testWidgets('scrolling never emits INPUT', (tester) async {
      final harness = await pumpTall(tester);
      for (final body in [0xFF, 0x01, 0x7F, 0xF6]) {
        await scroll(tester, harness, body);
      }
      expect(harness.inputs(), isEmpty);
    });
  });

  group('extractWheelScroll (mouse report parsing)', () {
    // xterm2 MouseReporter encodings (pinned 5.2.0): X10 `ESC[M btn col
    // row` (btn = 32 + id + mods), SGR `ESC[< Cb;Cx;Cy M/m` (Cb = id +
    // mods), urxvt `ESC[ Cb;Cx;Cy M` (Cb = 32 + id + mods). Wheel ids
    // are 64 up / 65 down; modifiers add 4/8/16, motion adds 32 — none
    // of which can forge the wheel bit from a click id (0-3).
    test('x10 wheel up/down counted and stripped', () {
      // Wheel ids 64/65 -> bytes 96/97 ('`'/'a').
      final r = extractWheelScroll('\x1b[M`!!\x1b[Ma!!');
      expect(r.up, 1);
      expect(r.down, 1);
      expect(r.remainder, isEmpty);
    });

    test('x10 click passes through untouched', () {
      const click = '\x1b[M !!'; // left down: id 0 -> byte 32
      final r = extractWheelScroll(click);
      expect(r.total, 0);
      expect(r.remainder, click);
    });

    test('sgr wheel with modifiers and release counted', () {
      // 64+shift(4)=68 up press; 65+ctrl(16)=81 down release.
      final r = extractWheelScroll('\x1b[<68;5;5M\x1b[<81;5;6m');
      expect(r.up, 1);
      expect(r.down, 1);
      expect(r.remainder, isEmpty);
    });

    test('sgr click and motion pass through', () {
      const s = '\x1b[<0;5;5M\x1b[<32;5;6M'; // left down; left motion (+32)
      final r = extractWheelScroll(s);
      expect(r.total, 0);
      expect(r.remainder, s);
    });

    test('urxvt wheel counted, horizontal kept', () {
      // Cb = 32 + id: 96 up, 97 down, 98 left.
      final r = extractWheelScroll('\x1b[96;5;5M\x1b[97;5;6M\x1b[98;5;7M');
      expect(r.up, 1);
      expect(r.down, 1);
      expect(r.remainder, '\x1b[98;5;7M');
    });

    test('mixed text and wheel keeps text', () {
      final r = extractWheelScroll('ab\x1b[<64;1;1Mc');
      expect(r.up, 1);
      expect(r.down, 0);
      expect(r.remainder, 'abc');
    });
  });

  group('extractWheelScroll wide coordinates (real xterm2 reporter)', () {
    // Vectors come from a real Terminal through mouseInput, so the
    // classic no-report case and the exact UTF encodings are pinned
    // reporter truth. Coordinates are 1-based, as the reporter sees them.
    String emitWheel(int col1, int row1,
        {String modeSeq = '', bool down = true}) {
      final t = Terminal(maxLines: 10);
      t.write('\x1b[?1000h$modeSeq');
      String? out;
      t.onOutput = (s) => out = (out ?? '') + s;
      t.mouseInput(
        down ? TerminalMouseButton.wheelDown : TerminalMouseButton.wheelUp,
        TerminalMouseButtonState.down,
        CellOffset(col1 - 1, row1 - 1),
      );
      return out ?? '';
    }

    test('classic emits at 223, nothing at 224/301 (col and row)', () {
      final at223 = emitWheel(223, 10);
      expect(at223, isNotEmpty, reason: 'classic col 223 emits');
      final r = extractWheelScroll(at223);
      expect(r.down, 1);
      expect(r.remainder, isEmpty);
      expect(emitWheel(224, 10), isEmpty, reason: 'classic col 224 silent');
      expect(emitWheel(301, 10), isEmpty, reason: 'classic col 301 silent');
      expect(emitWheel(10, 224), isEmpty, reason: 'classic row 224 silent');
      expect(emitWheel(301, 301), isEmpty);
    });

    test('utf consumes 224/301/2015, nothing at 2016', () {
      for (final c in [224, 301, 2015]) {
        final s = emitWheel(c, 10, modeSeq: '\x1b[?1005h');
        expect(s, isNotEmpty, reason: 'utf col $c emits');
        final r = extractWheelScroll(s);
        expect(r.down, 1, reason: 'utf col $c consumed');
        expect(r.remainder, isEmpty);
      }
      expect(emitWheel(2016, 10, modeSeq: '\x1b[?1005h'), isEmpty,
          reason: 'utf past 2015 silent');
    });

    test('utf wide row/up consumed, wide text and horizontal kept', () {
      final rowWide = emitWheel(10, 301, modeSeq: '\x1b[?1005h');
      expect(rowWide, isNotEmpty);
      final r = extractWheelScroll(rowWide);
      expect(r.down, 1);
      expect(r.remainder, isEmpty);
      final up = emitWheel(301, 301, modeSeq: '\x1b[?1005h', down: false);
      expect(up, isNotEmpty);
      final mixed = extractWheelScroll('ab${up}c');
      expect(mixed.up, 1);
      expect(mixed.down, 0);
      expect(mixed.remainder, 'abc');
      // Horizontal wheel at wide coords is not our axis: passes through.
      final t = Terminal(maxLines: 10);
      t.write('\x1b[?1000h\x1b[?1005h');
      String? hz;
      t.onOutput = (s) => hz = (hz ?? '') + s;
      t.mouseInput(TerminalMouseButton.wheelLeft, TerminalMouseButtonState.down,
          CellOffset(300, 300));
      expect(hz, isNotNull);
      final hr = extractWheelScroll(hz!);
      expect(hr.total, 0);
      expect(hr.remainder, hz);
    });

    test('sgr and urxvt consume wide coordinates', () {
      final sgr = emitWheel(301, 301, modeSeq: '\x1b[?1006h');
      expect(sgr, isNotEmpty);
      final sr = extractWheelScroll(sgr);
      expect(sr.down, 1);
      expect(sr.remainder, isEmpty);
      final urxvt = emitWheel(301, 301, modeSeq: '\x1b[?1015h');
      expect(urxvt, isNotEmpty);
      final ur = extractWheelScroll(urxvt);
      expect(ur.down, 1);
      expect(ur.remainder, isEmpty);
    });
  });

  group('normal-buffer mouse gestures drive local scroll', () {
    // xterm routes normal-buffer drag/wheel to application scrolling
    // when mouse reporting is on (dead local physics + mouse output
    // Python must reject). RelayDeck scrolls those gestures locally
    // instead: parsed per-line wheel output while controlling, raw
    // pointer deltas while watching. The emulator keeps its mouse
    // mode; consumed wheels never reach the PTY. Alternate-buffer
    // routing is unchanged (exact wheel / arrow fallback / swallow).
    Future<void> feed(
        WidgetTester tester, _GateHarness harness, String data) async {
      final bytes = utf8.encode(data);
      final packet = BytesBuilder(copy: false)
        ..addByte(0x01)
        ..add(_u32be(bytes.length))
        ..add(bytes);
      harness.factory.channel!.incoming.add(packet.toBytes());
      await tester.pump();
    }

    Future<_GateHarness> pumpTallMouse(WidgetTester tester,
        {required bool inputEnabled}) async {
      final harness = await _pumpGate(tester, inputEnabled: inputEnabled);
      final lines = List<String>.generate(
          100, (i) => 'row${i.toString().padLeft(3, '0')}');
      await feed(tester, harness, '${lines.join('\r\n')}\r\n');
      await feed(tester, harness, '\x1b[?1000h');
      await tester.pump();
      expect(harness.views().last & 0x04, 0x04, reason: 'mouse reporting on');
      expect(harness.views().last & 0x01, 0x00, reason: 'normal buffer');
      return harness;
    }

    ScrollPosition localPos(WidgetTester tester) {
      // The owned local scrollable: finite non-zero extent (the hidden
      // text field has zero extent; xterm's application scroller is
      // infinite while mouse reporting is on).
      final states = tester.stateList<ScrollableState>(
        find.descendant(
          of: find.byType(TerminalView),
          matching: find.byType(Scrollable),
        ),
      );
      final local = states
          .where((s) =>
              s.position.maxScrollExtent > 0 &&
              s.position.maxScrollExtent < double.infinity)
          .toList();
      expect(local, hasLength(1));
      return local.single.position;
    }

    Future<void> wheel(WidgetTester tester, double dy) async {
      final center = tester.getCenter(find.byType(TerminalView));
      await tester.sendEventToBinding(PointerScrollEvent(
        position: center,
        scrollDelta: Offset(0, dy),
      ));
      await tester.pump();
    }

    Future<void> pageUp(WidgetTester tester, _GateHarness harness) async {
      // Park mid-history: the harness tap focuses the view, whose
      // keyboard-show callback lands the fresh view at the bottom.
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x0B, 0xFF]));
      await tester.pump();
      await tester.pump();
    }

    testWidgets('control drag scrolls local lines, emits no PTY bytes',
        (tester) async {
      final harness = await pumpTallMouse(tester, inputEnabled: true);
      await pageUp(tester, harness);
      final before = localPos(tester).pixels;
      expect(before, lessThan(localPos(tester).maxScrollExtent));
      final inputsBefore = harness.inputs().length;
      await tester.drag(find.byType(TerminalView), const Offset(0, -300));
      await tester.pump();
      final moved = localPos(tester).pixels;
      expect(moved, greaterThan(before));
      expect(moved, lessThanOrEqualTo(before + 300));
      expect(harness.inputs().length, inputsBefore,
          reason: 'consumed wheels never reach the PTY');
      expect(harness.views().last & 0x02, 0x00);
    });

    testWidgets('control wheel scrolls local lines, emits no PTY bytes',
        (tester) async {
      final harness = await pumpTallMouse(tester, inputEnabled: true);
      await pageUp(tester, harness);
      final before = localPos(tester).pixels;
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 120);
      final moved = localPos(tester).pixels;
      expect(moved, greaterThan(before));
      expect(moved, lessThanOrEqualTo(before + 120));
      expect(harness.inputs().length, inputsBefore);
    });

    testWidgets('watching drag drives exact local pixels, emits nothing',
        (tester) async {
      final harness = await pumpTallMouse(tester, inputEnabled: false);
      await pageUp(tester, harness);
      final before = localPos(tester).pixels;
      await tester.drag(find.byType(TerminalView), const Offset(0, -300));
      await tester.pump();
      expect(localPos(tester).pixels, before + 300);
      expect(harness.inputs(), isEmpty);
    });

    testWidgets('watching wheel drives exact local pixels', (tester) async {
      final harness = await pumpTallMouse(tester, inputEnabled: false);
      await pageUp(tester, harness);
      final before = localPos(tester).pixels;
      await wheel(tester, 120);
      await tester.pump();
      expect(localPos(tester).pixels, before + 120);
      expect(harness.inputs(), isEmpty);
    });

    testWidgets('gesture scrolling reports at_bottom', (tester) async {
      final harness = await pumpTallMouse(tester, inputEnabled: false);
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x0B, 0x7F]));
      await tester.pump();
      await tester.pump();
      expect(harness.views().last & 0x02, 0x02);
      await tester.drag(find.byType(TerminalView), const Offset(0, 200));
      await tester.pump();
      expect(harness.views().last & 0x02, 0x00);
      await wheel(tester, 2000);
      await tester.pump();
      expect(harness.views().last & 0x02, 0x02);
    });

    testWidgets(
        'alt control drag emits exact wheel, moves nothing local',
        (tester) => _withPlatform(TargetPlatform.macOS, () async {
              final harness = await pumpTallMouse(tester, inputEnabled: true);
              final before = localPos(tester).pixels;
              await feed(tester, harness, '\x1b[?1049h');
              await tester.pump();
              expect(harness.views().last & 0x01, 0x01);
              final inputsBefore = harness.inputs().length;
              await tester.drag(
                  find.byType(TerminalView), const Offset(0, -300));
              await tester.pump();
              final fresh = harness.inputs().sublist(inputsBefore);
              expect(fresh, isNotEmpty);
              for (final p in fresh) {
                expect(p[0], 0x81);
                expect(p[1], 3, reason: 'mouse origin');
                final body = p.sublist(6);
                expect(body.length % 6, 0, reason: 'X10 reports are 6 bytes');
                for (var i = 0; i < body.length; i += 6) {
                  expect(body.sublist(i, i + 3), [0x1B, 0x5B, 0x4D]);
                  expect(body[i + 3], 97, reason: 'wheel down, no modifiers');
                }
              }
              await feed(tester, harness, '\x1b[?1049l');
              await tester.pump();
              expect(localPos(tester).pixels, before,
                  reason: 'no local movement in alt');
            }));

    testWidgets('alt watching drag writes nothing and reports nothing new',
        (tester) async {
      final harness = await pumpTallMouse(tester, inputEnabled: false);
      await feed(tester, harness, '\x1b[?1049h');
      await tester.pump();
      final reports = harness.views().length;
      await tester.drag(find.byType(TerminalView), const Offset(0, -300));
      await tester.pump();
      expect(harness.inputs(), isEmpty);
      expect(harness.views().length, reports);
    });

    testWidgets('engine and channel stable across gestures', (tester) async {
      final harness = await pumpTallMouse(tester, inputEnabled: true);
      Future<void> request(int reqId) async {
        final packet = BytesBuilder(copy: false)
          ..addByte(0x09)
          ..add(_u32be(reqId));
        harness.factory.channel!.incoming.add(packet.toBytes());
        await tester.pump();
      }

      await request(1);
      final before = _snapshots(harness)[1];
      await tester.drag(find.byType(TerminalView), const Offset(0, -300));
      await wheel(tester, 120);
      await tester.pump();
      await request(2);
      expect(_snapshots(harness)[2], before);
      expect(harness.factory.opens, 1, reason: 'single channel open');
    });
  });

  group('wide-coordinate normal-buffer gestures drive local scroll', () {
    // classic X10 emits NO report past 1-based coord 223 and
    // UTF reports there use wide chars, so gestures must not depend on
    // parsing reports. The always-mounted Listener drives the owned
    // controller from raw pointer deltas in both control and watch;
    // reports are consumed, never double-counted, never PTY-bound.
    // Each gesture lands in the bottom-right corner of a 310+ col/row
    // view (size asserted from SIZE_REPORT), so col AND row are wide.
    Future<void> feed(
        WidgetTester tester, _GateHarness harness, String data) async {
      final bytes = utf8.encode(data);
      final packet = BytesBuilder(copy: false)
        ..addByte(0x01)
        ..add(_u32be(bytes.length))
        ..add(bytes);
      harness.factory.channel!.incoming.add(packet.toBytes());
      await tester.pump();
    }

    Future<_GateHarness> pumpWide(WidgetTester tester,
        {required bool inputEnabled, required String modeSeq}) async {
      final harness = await _pumpGate(tester,
          inputEnabled: inputEnabled,
          width: 3000,
          height: 4500,
          fontSize: 8.0,
          maxLines: 2500);
      // Deep scrollback: the 450-row viewport needs well over one page
      // above it or pageUp parks within clamping distance of an edge.
      final lines = List<String>.generate(
          2000, (i) => 'row${i.toString().padLeft(4, '0')}');
      await feed(tester, harness, '${lines.join('\r\n')}\r\n');
      await feed(tester, harness, '\x1b[?1000h$modeSeq');
      await tester.pump();
      expect(harness.views().last & 0x04, 0x04, reason: 'mouse reporting on');
      expect(harness.views().last & 0x01, 0x00, reason: 'normal buffer');
      final size = harness.sizes().last;
      expect(size[0], greaterThanOrEqualTo(310),
          reason: 'cols admit col 301+ (got ${size[0]})');
      expect(size[1], greaterThanOrEqualTo(310),
          reason: 'rows admit row 301+ (got ${size[1]})');
      return harness;
    }

    ScrollPosition localPos(WidgetTester tester) {
      final states = tester.stateList<ScrollableState>(
        find.descendant(
          of: find.byType(TerminalView),
          matching: find.byType(Scrollable),
        ),
      );
      final local = states
          .where((s) =>
              s.position.maxScrollExtent > 0 &&
              s.position.maxScrollExtent < double.infinity)
          .toList();
      expect(local, hasLength(1));
      return local.single.position;
    }

    Offset corner(WidgetTester tester) {
      final rect = tester.getRect(find.byType(TerminalView));
      return Offset(rect.right - 2, rect.bottom - 2);
    }

    Future<void> wheelAt(WidgetTester tester, Offset pos, double dy) async {
      await tester.sendEventToBinding(PointerScrollEvent(
        position: pos,
        scrollDelta: Offset(0, dy),
      ));
      await tester.pump();
    }

    Future<void> pageUp(WidgetTester tester, _GateHarness harness) async {
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x0B, 0xFF]));
      await tester.pump();
      await tester.pump();
    }

    testWidgets('control drag at wide corner scrolls local (classic)',
        (tester) async {
      final harness = await pumpWide(tester, inputEnabled: true, modeSeq: '');
      await pageUp(tester, harness);
      expect(harness.views().last & 0x02, 0x00, reason: 'parked off bottom');
      final before = localPos(tester).pixels;
      final inputsBefore = harness.inputs().length;
      final c = corner(tester);
      await tester.dragFrom(c, const Offset(0, -300));
      await tester.pump();
      expect(localPos(tester).pixels, greaterThan(before));
      expect(harness.inputs().length, inputsBefore,
          reason: 'no PTY bytes at wide coords');
      await tester.dragFrom(c, const Offset(0, -20000));
      await tester.pump();
      expect(localPos(tester).pixels, localPos(tester).maxScrollExtent);
      expect(harness.views().last & 0x02, 0x02, reason: 'bottom reported');
      expect(harness.inputs().length, inputsBefore);
    });

    testWidgets('control wheel at wide corner scrolls local (classic)',
        (tester) async {
      final harness = await pumpWide(tester, inputEnabled: true, modeSeq: '');
      await pageUp(tester, harness);
      final before = localPos(tester).pixels;
      final inputsBefore = harness.inputs().length;
      final c = corner(tester);
      await wheelAt(tester, c, -300);
      expect(localPos(tester).pixels, lessThan(before));
      expect(harness.inputs().length, inputsBefore);
      await wheelAt(tester, c, 30000);
      await tester.pump();
      expect(localPos(tester).pixels, localPos(tester).maxScrollExtent);
      expect(harness.views().last & 0x02, 0x02);
      expect(harness.inputs().length, inputsBefore);
    });

    testWidgets('watching drag at wide corner scrolls local (classic)',
        (tester) async {
      final harness = await pumpWide(tester, inputEnabled: false, modeSeq: '');
      await pageUp(tester, harness);
      final before = localPos(tester).pixels;
      final c = corner(tester);
      await tester.dragFrom(c, const Offset(0, -300));
      await tester.pump();
      expect(localPos(tester).pixels, before + 300);
      expect(harness.inputs(), isEmpty);
      await tester.dragFrom(c, const Offset(0, -20000));
      await tester.pump();
      expect(localPos(tester).pixels, localPos(tester).maxScrollExtent);
      expect(harness.views().last & 0x02, 0x02);
      expect(harness.inputs(), isEmpty);
    });

    testWidgets('watching wheel at wide corner scrolls local (classic)',
        (tester) async {
      final harness = await pumpWide(tester, inputEnabled: false, modeSeq: '');
      await pageUp(tester, harness);
      final before = localPos(tester).pixels;
      final c = corner(tester);
      await wheelAt(tester, c, -300);
      expect(localPos(tester).pixels, before - 300);
      expect(harness.inputs(), isEmpty);
      await wheelAt(tester, c, 30000);
      await tester.pump();
      expect(localPos(tester).pixels, localPos(tester).maxScrollExtent);
      expect(harness.views().last & 0x02, 0x02);
      expect(harness.inputs(), isEmpty);
    });

    testWidgets('control drag at wide corner consumes reports (utf)',
        (tester) async {
      final harness =
          await pumpWide(tester, inputEnabled: true, modeSeq: '\x1b[?1005h');
      await pageUp(tester, harness);
      final before = localPos(tester).pixels;
      final inputsBefore = harness.inputs().length;
      final c = corner(tester);
      await tester.dragFrom(c, const Offset(0, -300));
      await tester.pump();
      expect(localPos(tester).pixels, greaterThan(before));
      expect(harness.inputs().length, inputsBefore,
          reason: 'wide utf reports consumed, not forwarded');
      await tester.dragFrom(c, const Offset(0, -20000));
      await tester.pump();
      expect(localPos(tester).pixels, localPos(tester).maxScrollExtent);
      expect(harness.views().last & 0x02, 0x02);
      expect(harness.inputs().length, inputsBefore);
    });

    testWidgets('control wheel at wide corner consumes reports (utf)',
        (tester) async {
      final harness =
          await pumpWide(tester, inputEnabled: true, modeSeq: '\x1b[?1005h');
      await pageUp(tester, harness);
      final before = localPos(tester).pixels;
      final inputsBefore = harness.inputs().length;
      final c = corner(tester);
      await wheelAt(tester, c, -300);
      expect(localPos(tester).pixels, lessThan(before));
      expect(harness.inputs().length, inputsBefore);
      await wheelAt(tester, c, 30000);
      await tester.pump();
      expect(localPos(tester).pixels, localPos(tester).maxScrollExtent);
      expect(harness.views().last & 0x02, 0x02);
      expect(harness.inputs().length, inputsBefore);
    });

    testWidgets('watching drag at wide corner scrolls local (utf)',
        (tester) async {
      final harness =
          await pumpWide(tester, inputEnabled: false, modeSeq: '\x1b[?1005h');
      await pageUp(tester, harness);
      final before = localPos(tester).pixels;
      final c = corner(tester);
      await tester.dragFrom(c, const Offset(0, -300));
      await tester.pump();
      expect(localPos(tester).pixels, before + 300);
      expect(harness.inputs(), isEmpty);
      await tester.dragFrom(c, const Offset(0, -20000));
      await tester.pump();
      expect(localPos(tester).pixels, localPos(tester).maxScrollExtent);
      expect(harness.views().last & 0x02, 0x02);
      expect(harness.inputs(), isEmpty);
    });

    testWidgets('watching wheel at wide corner scrolls local (utf)',
        (tester) async {
      final harness =
          await pumpWide(tester, inputEnabled: false, modeSeq: '\x1b[?1005h');
      await pageUp(tester, harness);
      final before = localPos(tester).pixels;
      final c = corner(tester);
      await wheelAt(tester, c, -300);
      expect(localPos(tester).pixels, before - 300);
      expect(harness.inputs(), isEmpty);
      await wheelAt(tester, c, 30000);
      await tester.pump();
      expect(localPos(tester).pixels, localPos(tester).maxScrollExtent);
      expect(harness.views().last & 0x02, 0x02);
      expect(harness.inputs(), isEmpty);
    });
  });

  group('symbol glyph fallback (Noto Sans Symbols 2)', () {
    // Agents emit dingbats/geometric/braille/status glyphs the mono
    // face lacks (agent TUI U+2733 titles, U+25D0 circle spinners,
    // braille spinners). The view lists the bundled symbols family
    // (page.fonts) between JetBrains Mono and monospace. This asserts
    // the configured fallback list, not renderer pixels.
    Future<void> feed(
        WidgetTester tester, _GateHarness harness, String data) async {
      final bytes = utf8.encode(data);
      final packet = BytesBuilder(copy: false)
        ..addByte(0x01)
        ..add(_u32be(bytes.length))
        ..add(bytes);
      harness.factory.channel!.incoming.add(packet.toBytes());
      await tester.pump();
    }

    testWidgets('terminal view carries the symbols fallback family',
        (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      final view = tester.widget<TerminalView>(find.byType(TerminalView));
      expect(view.textStyle.fontFamilyFallback, [
        'JetBrains Mono',
        'Noto Sans Symbols 2',
        'monospace',
      ]);
      // The full glyph set survives the write path unmangled.
      await feed(tester, harness, '✳ ◐ ⠋ ⚠ ⏺');
      final request = BytesBuilder(copy: false)
        ..addByte(0x09)
        ..add(_u32be(1));
      harness.factory.channel!.incoming.add(request.toBytes());
      await tester.pump();
      expect(_snapshots(harness)[1], '✳ ◐ ⠋ ⚠ ⏺');
      expect(harness.inputs(), isEmpty, reason: 'output never echoes');
    });
  });

  group('alt-buffer no-mouse swipes emit swipe origin', () {
    // xterm2's arrow fallback is disabled (simulateScroll: false): in the
    // alternate screen without mouse reporting, Listener drag/wheel
    // deltas emit INPUT origin 4 (direction + line count) instead of
    // Up/Down arrows, so agents never see prompt-history keys from a
    // swipe. Physical keys stay ORIGIN_KEY, mouse reporting stays
    // ORIGIN_MOUSE, normal-buffer scrolling is untouched.
    Future<void> feed(
        WidgetTester tester, _GateHarness harness, String data) async {
      final bytes = utf8.encode(data);
      final packet = BytesBuilder(copy: false)
        ..addByte(0x01)
        ..add(_u32be(bytes.length))
        ..add(bytes);
      harness.factory.channel!.incoming.add(packet.toBytes());
      await tester.pump();
    }

    Future<_GateHarness> pumpAltNoMouse(WidgetTester tester,
        {required bool inputEnabled}) async {
      final harness = await _pumpGate(tester, inputEnabled: inputEnabled);
      await feed(tester, harness, '\x1b[?1049h');
      await tester.pump();
      expect(harness.views().last & 0x01, 0x01, reason: 'alt buffer');
      expect(harness.views().last & 0x04, 0x00, reason: 'no mouse mode');
      return harness;
    }

    Future<void> wheel(WidgetTester tester, double dy) async {
      final center = tester.getCenter(find.byType(TerminalView));
      await tester.sendEventToBinding(PointerScrollEvent(
        position: center,
        scrollDelta: Offset(0, dy),
      ));
      await tester.pump();
    }

    /// Parses fresh origin-4 packets as (direction, count), asserting the
    /// exact payload shape. Fails on any non-swipe INPUT packet.
    List<(int, int)> swipes(_GateHarness harness, int inputsBefore) {
      final fresh = harness.inputs().sublist(inputsBefore);
      expect(fresh, isNotEmpty, reason: 'swipe emits packets');
      final out = <(int, int)>[];
      for (final p in fresh) {
        expect(p[0], 0x81);
        expect(p[1], 4, reason: 'swipe origin, never key/mouse/text');
        final len = ByteData.sublistView(p, 2, 6).getUint32(0, Endian.big);
        expect(len, 2, reason: 'direction + count only');
        final dir = p[6], count = p[7];
        expect(dir == 0 || dir == 1, isTrue);
        expect(count, inInclusiveRange(1, 255));
        out.add((dir, count));
      }
      return out;
    }

    testWidgets('simulateScroll fallback is disabled', (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final view = tester.widget<TerminalView>(find.byType(TerminalView));
      expect(view.simulateScroll, isFalse);
      expect(harness.inputs(), isEmpty);
    });

    testWidgets('drag up emits swipe down, no arrow bytes', (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await tester.drag(find.byType(TerminalView), const Offset(0, -300));
      await tester.pump();
      final got = swipes(harness, inputsBefore);
      expect(got, hasLength(1), reason: 'one release flush under a page');
      expect(got.single.$1, 1, reason: 'finger up scrolls toward latest');
    });

    testWidgets('drag down emits swipe up', (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await tester.drag(find.byType(TerminalView), const Offset(0, 300));
      await tester.pump();
      final got = swipes(harness, inputsBefore);
      expect(got, hasLength(1));
      expect(got.single.$1, 0, reason: 'finger down reveals older output');
    });

    testWidgets('viewport drag emits a page packet plus release flush',
        (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final rows = harness.sizes().last[1];
      final inputsBefore = harness.inputs().length;
      await tester.drag(find.byType(TerminalView), const Offset(0, -700));
      await tester.pump();
      final got = swipes(harness, inputsBefore);
      expect(got.length, 2);
      expect(got[0], (1, rows), reason: 'full page mid-drag');
      expect(got[1].$1, 1, reason: 'remainder flush keeps direction');
    });

    testWidgets(
        'physical arrows stay ORIGIN_KEY with exact bytes',
        (tester) => _withPlatform(TargetPlatform.macOS, () async {
              final harness = await pumpAltNoMouse(tester, inputEnabled: true);
              final inputsBefore = harness.inputs().length;
              await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
              await tester.pump();
              await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
              await tester.pump();
              final fresh = harness.inputs().sublist(inputsBefore);
              expect(fresh, hasLength(2));
              expect(fresh[0][1], 2);
              expect(fresh[0].sublist(6), [0x1B, 0x5B, 0x41]);
              expect(fresh[1][1], 2);
              expect(fresh[1].sublist(6), [0x1B, 0x5B, 0x42]);
            }));

    testWidgets(
        'alt mouse drag still emits exact wheel, no swipe',
        (tester) => _withPlatform(TargetPlatform.macOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              await feed(tester, harness, '\x1b[?1000h\x1b[?1049h');
              await tester.pump();
              expect(harness.views().last & 0x05, 0x05);
              final inputsBefore = harness.inputs().length;
              await tester.drag(
                  find.byType(TerminalView), const Offset(0, -300));
              await tester.pump();
              final fresh = harness.inputs().sublist(inputsBefore);
              expect(fresh, isNotEmpty);
              for (final p in fresh) {
                expect(p[1], 3, reason: 'mouse origin unchanged');
                expect(p.sublist(6, 9), [0x1B, 0x5B, 0x4D]);
              }
            }));

    testWidgets(
        'touch: swipes still emit while focus is not armed',
        (tester) => _withPlatform(TargetPlatform.iOS, () async {
              final harness = await pumpAltNoMouse(tester, inputEnabled: true);
              expect(
                  tester
                      .widget<TerminalView>(find.byType(TerminalView))
                      .focusNode!
                      .canRequestFocus,
                  isFalse,
                  reason: 'touch focus stays gated until armed');
              final inputsBefore = harness.inputs().length;
              await tester.drag(
                  find.byType(TerminalView), const Offset(0, -300));
              await tester.pump();
              final got = swipes(harness, inputsBefore);
              expect(got, hasLength(1),
                  reason: 'the bridge Listener, not the mouse path');
              expect(got.single.$1, 1,
                  reason: 'finger up scrolls toward latest');
            }));

    testWidgets('normal-buffer drag scrolls natively, emits nothing',
        (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      final lines = List<String>.generate(
          100, (i) => 'row${i.toString().padLeft(3, '0')}');
      await feed(tester, harness, '${lines.join('\r\n')}\r\n');
      await tester.pump();
      // Park mid-history: the fresh view sits at the bottom.
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x0B, 0xFF]));
      await tester.pump();
      await tester.pump();
      ScrollPosition pos() => tester
          .stateList<ScrollableState>(
            find.descendant(
              of: find.byType(TerminalView),
              matching: find.byType(Scrollable),
            ),
          )
          .single
          .position;
      final before = pos().pixels;
      final inputsBefore = harness.inputs().length;
      await tester.drag(find.byType(TerminalView), const Offset(0, -300));
      await tester.pump();
      expect(pos().pixels, greaterThan(before));
      expect(harness.inputs().length, inputsBefore);
    });

    testWidgets('watching alt drag emits nothing', (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: false);
      await tester.drag(find.byType(TerminalView), const Offset(0, -300));
      await tester.pump();
      expect(harness.inputs(), isEmpty);
    });

    testWidgets('wheel emits swipe down after the idle flush', (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 120);
      await tester.pump(const Duration(milliseconds: 200));
      final got = swipes(harness, inputsBefore);
      expect(got, hasLength(1));
      expect(got.single.$1, 1, reason: 'wheel down scrolls toward latest');
    });

    testWidgets('slow wheel steps accumulate into whole lines only',
        (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      var firstEmit = -1;
      for (var i = 1; i <= 12; i++) {
        await wheel(tester, 5);
        await tester.pump(const Duration(milliseconds: 200));
        if (firstEmit < 0 && harness.inputs().length > inputsBefore) {
          firstEmit = i;
        }
      }
      expect(firstEmit, greaterThan(1),
          reason: 'one 5 px wheel never emits by itself');
      final got = swipes(harness, inputsBefore);
      expect(got.every((s) => s.$1 == 1), isTrue,
          reason: 'every packet goes toward latest, none reversed');
      expect(got.first, (1, 1));
      final total = got.fold<int>(0, (sum, s) => sum + s.$2);
      expect(total, inInclusiveRange(2, 5));
    });

    testWidgets('a single 5 px wheel emits nothing', (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 5);
      await tester.pump(const Duration(milliseconds: 400));
      expect(harness.inputs().length, inputsBefore);
    });

    testWidgets('direction flip commits separate packets', (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      final center = tester.getCenter(find.byType(TerminalView));
      final gesture = await tester.startGesture(center);
      await gesture.moveBy(const Offset(0, -100));
      await gesture.moveBy(const Offset(0, 250));
      await gesture.up();
      await tester.pump();
      final got = swipes(harness, inputsBefore);
      expect(got.map((s) => s.$1), containsAll([0, 1]));
    });
  });

  group('swipe boundary retirement', () {
    // Pending swipe pixels, the idle timer and the active pointer are
    // retired at input-disable, RESET and alt/mouse transitions. Delayed
    // callbacks fire only for the boundary generation that armed them;
    // a fresh pointer-down is required after retirement.
    Future<void> feed(
        WidgetTester tester, _GateHarness harness, String data) async {
      final bytes = utf8.encode(data);
      final packet = BytesBuilder(copy: false)
        ..addByte(0x01)
        ..add(_u32be(bytes.length))
        ..add(bytes);
      harness.factory.channel!.incoming.add(packet.toBytes());
      await tester.pump();
    }

    Future<_GateHarness> pumpAltNoMouse(WidgetTester tester,
        {required bool inputEnabled}) async {
      final harness = await _pumpGate(tester, inputEnabled: inputEnabled);
      await feed(tester, harness, '\x1b[?1049h');
      await tester.pump();
      expect(harness.views().last & 0x01, 0x01, reason: 'alt buffer');
      expect(harness.views().last & 0x04, 0x00, reason: 'no mouse mode');
      return harness;
    }

    Future<void> wheel(WidgetTester tester, double dy) async {
      final center = tester.getCenter(find.byType(TerminalView));
      await tester.sendEventToBinding(PointerScrollEvent(
        position: center,
        scrollDelta: Offset(0, dy),
      ));
      await tester.pump();
    }

    Future<void> setInput(
        WidgetTester tester, _GateHarness harness, bool enabled) async {
      harness.factory.channel!.incoming
          .add(Uint8List.fromList([0x03, enabled ? 0x01 : 0x00]));
      await tester.pump();
    }

    List<Uint8List> origin4(_GateHarness harness, int inputsBefore) => harness
        .inputs()
        .sublist(inputsBefore)
        .where((p) => p.length > 1 && p[0] == 0x81 && p[1] == 4)
        .toList();

    testWidgets('disable before idle flush emits nothing', (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 120);
      await setInput(tester, harness, false);
      await tester.pump(const Duration(milliseconds: 200));
      expect(harness.inputs().length, inputsBefore);
    });

    testWidgets('disable-enable inside the idle window still drops',
        (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 120);
      await setInput(tester, harness, false);
      await setInput(tester, harness, true);
      await tester.pump(const Duration(milliseconds: 200));
      expect(harness.inputs().length, inputsBefore,
          reason: 'epoch fence, not just a current-eligibility recheck');
      // A fresh wheel after the boundary still works.
      await wheel(tester, 120);
      await tester.pump(const Duration(milliseconds: 200));
      expect(origin4(harness, inputsBefore), hasLength(1));
    });

    testWidgets('mode round trip inside the idle window still drops',
        (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 120);
      await feed(tester, harness, '\x1b[?1049l');
      await feed(tester, harness, '\x1b[?1049h');
      await tester.pump(const Duration(milliseconds: 200));
      expect(harness.inputs().length, inputsBefore);
    });

    testWidgets('held drag across disable-enable emits nothing',
        (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      final center = tester.getCenter(find.byType(TerminalView));
      final gesture = await tester.startGesture(center);
      await gesture.moveBy(const Offset(0, -100));
      await setInput(tester, harness, false);
      await setInput(tester, harness, true);
      await gesture.moveBy(const Offset(0, -100));
      await gesture.up();
      await tester.pump();
      expect(harness.inputs().length, inputsBefore,
          reason: 'retirement requires a new pointer-down');
      // A fresh drag after the boundary still works.
      await tester.drag(find.byType(TerminalView), const Offset(0, -300));
      await tester.pump();
      expect(origin4(harness, inputsBefore), hasLength(1));
    });

    testWidgets('reset retires the pending wheel run', (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 120);
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x08]));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 200));
      expect(harness.inputs().length, inputsBefore);
      // A fresh gesture in a re-entered alt screen still works.
      await feed(tester, harness, '\x1b[?1049h');
      await tester.pump();
      await tester.drag(find.byType(TerminalView), const Offset(0, -300));
      await tester.pump();
      expect(origin4(harness, inputsBefore), hasLength(1));
    });

    testWidgets('mouse round trip with held drag emits nothing',
        (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      final center = tester.getCenter(find.byType(TerminalView));
      final gesture = await tester.startGesture(center);
      await gesture.moveBy(const Offset(0, -100));
      await feed(tester, harness, '\x1b[?1000h');
      await feed(tester, harness, '\x1b[?1000l');
      await gesture.moveBy(const Offset(0, -100));
      await gesture.up();
      await tester.pump();
      expect(harness.inputs().length, inputsBefore);
      // A fresh drag after the boundary still works.
      await tester.drag(find.byType(TerminalView), const Offset(0, -300));
      await tester.pump();
      expect(origin4(harness, inputsBefore), hasLength(1));
    });

    testWidgets('disposal cancels the idle timer', (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 120);
      await tester.pumpWidget(const SizedBox());
      await tester.pump(const Duration(milliseconds: 200));
      expect(tester.takeException(), isNull);
      expect(harness.inputs().length, inputsBefore);
    });
  });

  group('coalesced/synchronized swipe retirement', () {
    // xterm2 parses a whole write() before notifying, and suppresses
    // notifications inside synchronized output (ESC[?2026h..l), so the
    // repaint listener misses a leave-and-return in one write or across
    // synchronized writes. The tagged terminal observes the actual
    // mode/buffer mutations instead and retires immediately.
    Future<void> feed(
        WidgetTester tester, _GateHarness harness, String data) async {
      final bytes = utf8.encode(data);
      final packet = BytesBuilder(copy: false)
        ..addByte(0x01)
        ..add(_u32be(bytes.length))
        ..add(bytes);
      harness.factory.channel!.incoming.add(packet.toBytes());
      await tester.pump();
    }

    Future<_GateHarness> pumpAltNoMouse(WidgetTester tester,
        {required bool inputEnabled}) async {
      final harness = await _pumpGate(tester, inputEnabled: inputEnabled);
      await feed(tester, harness, '\x1b[?1049h');
      await tester.pump();
      expect(harness.views().last & 0x01, 0x01, reason: 'alt buffer');
      expect(harness.views().last & 0x04, 0x00, reason: 'no mouse mode');
      return harness;
    }

    Future<void> wheel(WidgetTester tester, double dy) async {
      final center = tester.getCenter(find.byType(TerminalView));
      await tester.sendEventToBinding(PointerScrollEvent(
        position: center,
        scrollDelta: Offset(0, dy),
      ));
      await tester.pump();
    }

    List<Uint8List> origin4(_GateHarness harness, int inputsBefore) => harness
        .inputs()
        .sublist(inputsBefore)
        .where((p) => p.length > 1 && p[0] == 0x81 && p[1] == 4)
        .toList();

    testWidgets('one-write alt round trip drops the pending run',
        (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 120);
      // Leave-and-return inside a single write(): one notification, if
      // any, sees the original bits again.
      await feed(tester, harness, '\x1b[?1049l\x1b[?1049h');
      await tester.pump(const Duration(milliseconds: 200));
      expect(harness.inputs().length, inputsBefore);
      // A fresh wheel after the boundary still works.
      await wheel(tester, 120);
      await tester.pump(const Duration(milliseconds: 200));
      expect(origin4(harness, inputsBefore), hasLength(1));
    });

    testWidgets('one-write mouse round trip drops the pending run',
        (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 120);
      await feed(tester, harness, '\x1b[?1000h\x1b[?1000l');
      await tester.pump(const Duration(milliseconds: 200));
      expect(harness.inputs().length, inputsBefore);
      await wheel(tester, 120);
      await tester.pump(const Duration(milliseconds: 200));
      expect(origin4(harness, inputsBefore), hasLength(1));
    });

    testWidgets('synchronized separate writes drop the pending run',
        (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 120);
      await feed(tester, harness, '\x1b[?2026h');
      await feed(tester, harness, '\x1b[?1049l');
      await feed(tester, harness, '\x1b[?1049h');
      await feed(tester, harness, '\x1b[?2026l');
      await tester.pump(const Duration(milliseconds: 200));
      expect(harness.inputs().length, inputsBefore);
      await wheel(tester, 120);
      await tester.pump(const Duration(milliseconds: 200));
      expect(origin4(harness, inputsBefore), hasLength(1));
    });

    testWidgets('held drag across a one-write round trip emits nothing',
        (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      final center = tester.getCenter(find.byType(TerminalView));
      final gesture = await tester.startGesture(center);
      await gesture.moveBy(const Offset(0, -100));
      await feed(tester, harness, '\x1b[?1049l\x1b[?1049h');
      await gesture.moveBy(const Offset(0, -100));
      await gesture.up();
      await tester.pump();
      expect(harness.inputs().length, inputsBefore,
          reason: 'retirement requires a new pointer-down');
      // A fresh drag after the boundary still works.
      await tester.drag(find.byType(TerminalView), const Offset(0, -300));
      await tester.pump();
      expect(origin4(harness, inputsBefore), hasLength(1));
    });

    testWidgets('redundant mode set keeps the pending run', (tester) async {
      final harness = await pumpAltNoMouse(tester, inputEnabled: true);
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 120);
      // No-op re-emission: already alt, already no-mouse. Ordinary and
      // redundant output must not retire an in-progress gesture.
      await feed(tester, harness, '\x1b[?1049hplain text\x1b[?1000l');
      await tester.pump(const Duration(milliseconds: 200));
      expect(origin4(harness, inputsBefore), hasLength(1));
    });
  });

  group('transport resume: hello, probe, input fence', () {
    // After authenticated session reuse Python rebinds this same engine to
    // the replacement transport. The engine announces its identity
    // once per mount (hello, req 0), answers resume probes with live
    // (instance, resets, applied) without touching engine state, and
    // drops user input while a live transport is reconnecting.
    Future<void> feed(
        WidgetTester tester, _GateHarness harness, String data) async {
      final bytes = utf8.encode(data);
      final packet = BytesBuilder(copy: false)
        ..addByte(0x01)
        ..add(_u32be(bytes.length))
        ..add(bytes);
      harness.factory.channel!.incoming.add(packet.toBytes());
      await tester.pump();
    }

    Future<void> probe(
        WidgetTester tester, _GateHarness harness, int req) async {
      final packet = BytesBuilder(copy: false)
        ..addByte(0x0C)
        ..add(_u32be(req));
      harness.factory.channel!.incoming.add(packet.toBytes());
      await tester.pump();
    }

    /// Parses every 0x87 packet as (req, instance, resets, applied),
    /// asserting the exact 25-byte shape. u64 halves avoid getUint64
    /// (throws on dart2js, like setUint64).
    List<(int, int, int, int)> reports(_GateHarness harness) {
      final out = <(int, int, int, int)>[];
      for (final p in harness.factory.channel!.sent) {
        if (p.isEmpty || p[0] != 0x87) continue;
        expect(p.length, 25, reason: 'kind + u32 + u64 + u32 + u64');
        final view = ByteData.sublistView(p);
        out.add((
          view.getUint32(1, Endian.big),
          view.getUint32(5, Endian.big) * 0x100000000 +
              view.getUint32(9, Endian.big),
          view.getUint32(13, Endian.big),
          view.getUint32(17, Endian.big) * 0x100000000 +
              view.getUint32(21, Endian.big),
        ));
      }
      return out;
    }

    testWidgets('mount sends the hello once with zero counters',
        (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      final hello = reports(harness);
      expect(hello, hasLength(1));
      expect(hello.single.$1, 0, reason: 'req 0 is the hello');
      expect(hello.single.$3, 0, reason: 'no resets at mount');
      expect(hello.single.$4, 0, reason: 'nothing applied at mount');
      await feed(tester, harness, 'output bytes');
      await tester.pump();
      expect(reports(harness), hasLength(1), reason: 'hello is once');
    });

    testWidgets('probe is answered with live applied bytes', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      final instance = reports(harness).single.$2;
      await feed(tester, harness, 'x' * 100);
      await probe(tester, harness, 1);
      final got = reports(harness);
      expect(got, hasLength(2));
      expect(got.last, (1, instance, 0, 100));
      // Probing is a pure report: no engine output consumed or moved.
      await probe(tester, harness, 2);
      expect(reports(harness).last, (2, instance, 0, 100));
    });

    testWidgets('reset count increments on applied RESET', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      final instance = reports(harness).single.$2;
      await feed(tester, harness, '0123456789');
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x08]));
      await tester.pump();
      await probe(tester, harness, 1);
      expect(reports(harness).last, (1, instance, 1, 0));
      await feed(tester, harness, 'abcde');
      await probe(tester, harness, 2);
      expect(reports(harness).last, (2, instance, 1, 5));
    });

    testWidgets('remount mints a fresh instance', (tester) async {
      final first = await _pumpGate(tester, inputEnabled: true);
      final firstInstance = reports(first).single.$2;
      await tester.pumpWidget(const SizedBox());
      final second = await _pumpGate(tester, inputEnabled: true);
      final hello = reports(second);
      expect(hello, hasLength(1));
      expect(hello.single.$2 == firstInstance, isFalse,
          reason: 'a new engine mount is never the old instance');
      expect((hello.single.$3, hello.single.$4), (0, 0));
    });

    testWidgets('reserved req-0 probe is ignored', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await probe(tester, harness, 0);
      expect(reports(harness), hasLength(1), reason: 'hello only');
    });

    Future<void> verify(WidgetTester tester, _GateHarness harness) async {
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x0D]));
      await tester.pump();
    }

    for (final recovery in [0x08, 0x0D]) {
      testWidgets('geometry resend after recovery $recovery', (tester) async {
        final harness = await _pumpGate(tester, inputEnabled: true);
        final backend = harness.control.backend;
        backend.isLoading = false;
        backend.notifyListeners();
        await tester.pump();
        final original = harness.sizes().last;
        backend.isLoading = true;
        backend.notifyListeners();
        await tester.pump();
        await tester.binding.setSurfaceSize(const Size(390, 420));
        await tester.pumpAndSettle();
        final current = harness.sizes().last;
        expect(current, isNot(original), reason: 'layout changed while fenced');
        // Python rejects the reports during this interval. No later layout
        // change is available to fix the remote geometry after continuity.
        backend.isLoading = false;
        backend.notifyListeners();
        await tester.pumpAndSettle();
        final before = harness.sizes().length;
        final inputs = harness.inputs().length;
        harness.factory.channel!.incoming.add(Uint8List.fromList([recovery]));
        await tester.pumpAndSettle();
        expect(harness.sizes().sublist(before), [current]);
        await tester.pump();
        expect(harness.sizes().length, before + 1,
            reason: 'one resend; unchanged layout stays deduplicated');
        expect(harness.inputs().length, inputs, reason: 'no PTY input replay');
        expect(harness.factory.opens, 1, reason: 'retained native ancestry');
      });
    }

    testWidgets('geometry resend once when input authority returns',
        (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: false);
      final current = harness.sizes().last;
      final before = harness.sizes().length;
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x03, 1]));
      await tester.pumpAndSettle();
      expect(harness.sizes().sublist(before), [current]);
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x03, 1]));
      await tester.pumpAndSettle();
      expect(harness.sizes().length, before + 1,
          reason: 'duplicate enabled packet is not an authority transition');
      expect(harness.inputs(), isEmpty);
    });

    testWidgets('input authority cannot resend geometry before verification',
        (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: false);
      final backend = harness.control.backend;
      backend.isLoading = false;
      backend.notifyListeners();
      await tester.pump();
      backend.isLoading = true;
      backend.notifyListeners();
      await tester.pump();
      final before = harness.sizes().length;
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x03, 1]));
      await tester.pumpAndSettle();
      expect(harness.sizes().length, before);
      await verify(tester, harness);
      expect(harness.sizes().length, before + 1);
      expect(harness.inputs(), isEmpty);
    });

    testWidgets(
        'native replacement reconstructs expected screen from fresh output',
        (tester) async {
      final first = await _pumpGate(tester, inputEnabled: true);
      final oldInstance = reports(first).single.$2;
      await feed(tester, first, 'old screen');
      await tester.pumpWidget(const SizedBox());
      final fresh = await _pumpGate(tester, inputEnabled: false);
      expect(reports(fresh).single.$2, isNot(oldInstance));
      fresh.factory.channel!.incoming.add(Uint8List.fromList([0x08]));
      await tester.pump();
      await feed(tester, fresh, 'reconstructed greeting\r\nnext line');
      fresh.factory.channel!.incoming.add((BytesBuilder(copy: false)
            ..addByte(0x09)
            ..add(_u32be(7)))
          .toBytes());
      await tester.pump();
      expect(_snapshots(fresh)[7], 'reconstructed greeting\nnext line');
      expect(fresh.inputs(), isEmpty);
    });

    Future<void> wheel(WidgetTester tester, double dy) async {
      final center = tester.getCenter(find.byType(TerminalView));
      await tester.sendEventToBinding(PointerScrollEvent(
        position: center,
        scrollDelta: Offset(0, dy),
      ));
      await tester.pump();
    }

    List<Uint8List> origin4(_GateHarness harness, int inputsBefore) => harness
        .inputs()
        .sublist(inputsBefore)
        .where((p) => p.length > 1 && p[0] == 0x81 && p[1] == 4)
        .toList();

    testWidgets(
        'input stays fenced until verified',
        (tester) => _withPlatform(TargetPlatform.macOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              final backend = harness.control.backend;
              backend.isLoading = false;
              backend.notifyListeners();
              await tester.pump();
              final baseline = harness.inputs().length;
              await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
              await tester.pump();
              expect(harness.inputs().length, baseline + 1,
                  reason: 'live flows');
              // Transport drops: the same keystroke is dropped, never queued.
              backend.isLoading = true;
              backend.notifyListeners();
              await tester.pump();
              await tester.sendKeyEvent(LogicalKeyboardKey.keyB);
              await tester.pump();
              expect(harness.inputs().length, baseline + 1, reason: 'fenced');
              // Re-registered but unverified: still fenced. UI plumbing is
              // not continuity proof.
              backend.isLoading = false;
              backend.notifyListeners();
              await tester.pump();
              await tester.pump(const Duration(milliseconds: 200));
              await tester.sendKeyEvent(LogicalKeyboardKey.keyC);
              await tester.pump();
              expect(harness.inputs().length, baseline + 1,
                  reason: 'latch survives re-registration');
              // Verified resume: input flows again with no replay of fenced keys.
              await verify(tester, harness);
              await tester.sendKeyEvent(LogicalKeyboardKey.keyD);
              await tester.pump();
              final fresh = harness.inputs().sublist(baseline);
              expect(fresh, hasLength(2));
              expect(fresh[0].sublist(6), utf8.encode('a'));
              expect(fresh[1].sublist(6), utf8.encode('d'));
            }));

    testWidgets(
        'RESET releases the transport fence',
        (tester) => _withPlatform(TargetPlatform.macOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              final backend = harness.control.backend;
              backend.isLoading = false;
              backend.notifyListeners();
              await tester.pump();
              backend.isLoading = true;
              backend.notifyListeners();
              await tester.pump();
              backend.isLoading = false;
              backend.notifyListeners();
              await tester.pump();
              final baseline = harness.inputs().length;
              await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
              await tester.pump();
              expect(harness.inputs().length, baseline, reason: 'still fenced');
              // Explicit resync from Python: both sides restart from a known
              // base, so input flows again.
              harness.factory.channel!.incoming.add(Uint8List.fromList([0x08]));
              await tester.pump();
              await tester.sendKeyEvent(LogicalKeyboardKey.keyB);
              await tester.pump();
              expect(harness.inputs().length, baseline + 1);
            }));

    testWidgets('transport edge retires pending swipe', (tester) async {
      final harness = await _pumpGate(tester, inputEnabled: true);
      await feed(tester, harness, '\x1b[?1049h');
      await tester.pump();
      final backend = harness.control.backend;
      backend.isLoading = false;
      backend.notifyListeners();
      await tester.pump();
      final inputsBefore = harness.inputs().length;
      await wheel(tester, 120);
      // The drop edge retires the pending run: the idle flush and any
      // release afterwards emit nothing, even after re-registration.
      backend.isLoading = true;
      backend.notifyListeners();
      await tester.pump();
      backend.isLoading = false;
      backend.notifyListeners();
      await tester.pump(const Duration(milliseconds: 200));
      expect(harness.inputs().length, inputsBefore,
          reason: 'old gesture cannot emit after reopening');
      // Verified resume plus a fresh gesture works.
      harness.factory.channel!.incoming.add(Uint8List.fromList([0x0D]));
      await tester.pump();
      await wheel(tester, 120);
      await tester.pump(const Duration(milliseconds: 200));
      expect(origin4(harness, inputsBefore), hasLength(1));
    });

    testWidgets(
        'input flows when the transport was never live',
        (tester) => _withPlatform(TargetPlatform.macOS, () async {
              final harness = await _pumpGate(tester, inputEnabled: true);
              expect(harness.control.backend.isLoading, isTrue);
              final baseline = harness.inputs().length;
              await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
              await tester.pump();
              expect(harness.inputs().length, baseline + 1,
                  reason: 'boot parity: only a live->reconnecting edge fences');
            }));
  });
}

class _FakeDataChannel implements DataChannel {
  _FakeDataChannel(this.id);

  @override
  final int id;
  final sent = <Uint8List>[];
  final incoming = StreamController<Uint8List>.broadcast();
  var closed = false;

  @override
  Stream<Uint8List> get messages => incoming.stream;

  @override
  bool send(Uint8List message) {
    sent.add(message);
    return true;
  }

  @override
  void close() {
    closed = true;
    incoming.close();
  }
}

class _FakeDataChannelFactory implements DataChannelFactory {
  _FakeDataChannel? channel;
  var opens = 0;

  @override
  DataChannel open() {
    opens++;
    channel = _FakeDataChannel(7);
    return channel!;
  }
}

class _GateHarness {
  _GateHarness({required this.factory, required this.control});

  final _FakeDataChannelFactory factory;
  final Control control;

  /// TERMINAL_INPUT (0x81) packets forwarded to the Python side.
  List<Uint8List> inputs() =>
      factory.channel!.sent.where((p) => p.isNotEmpty && p[0] == 0x81).toList();

  /// MODE_REPORT (0x85) payloads: live engine bracketed-paste mode.
  List<bool> modes() => factory.channel!.sent
      .where((p) => p.isNotEmpty && p[0] == 0x85)
      .map((p) => p.length > 1 && p[1] == 0x01)
      .toList();

  /// VIEW_REPORT (0x86) payload bytes: bit0 alt, bit1 at bottom,
  /// bit2 mouse reporting on.
  List<int> views() => factory.channel!.sent
      .where((p) => p.length == 2 && p[0] == 0x86)
      .map((p) => p[1])
      .toList();

  /// KEYBOARD_STATE (0x88) reports: [0x88, 1] shown, [0x88, 0] hidden.
  List<Uint8List> keyboardStates() => factory.channel!.sent
      .where((p) => p.length == 2 && p[0] == 0x88)
      .toList();

  /// SIZE_REPORT (0x82) payloads as [cols, rows] pairs.
  List<List<int>> sizes() => factory.channel!.sent
          .where((p) => p.length == 5 && p[0] == 0x82)
          .map((p) {
        final view = ByteData.sublistView(p, 1);
        return [
          view.getUint16(0, Endian.big),
          view.getUint16(2, Endian.big),
        ];
      }).toList();

  /// ACK (0xFF) cumulative applied bytes, decoded as two u32be halves
  /// (getUint64 is avoided: it throws on dart2js, like setUint64).
  List<int> acks() => factory.channel!.sent
          .where((p) => p.length == 9 && p[0] == 0xFF)
          .map((p) {
        final view = ByteData.sublistView(p, 1);
        return view.getUint32(0, Endian.big) * 0x100000000 +
            view.getUint32(4, Endian.big);
      }).toList();
}

Uint8List _u32be(int v) =>
    (ByteData(4)..setUint32(0, v, Endian.big)).buffer.asUint8List();

class _SnapshotChunk {
  _SnapshotChunk(this.reqId, this.total, this.offset, this.data);

  final int reqId;
  final int total;
  final int offset;
  final Uint8List data;
}

List<_SnapshotChunk> _snapshotChunks(_GateHarness harness) {
  final out = <_SnapshotChunk>[];
  for (final p in harness.factory.channel!.sent) {
    if (p.isEmpty || p[0] != 0x84) continue;
    final view = ByteData.sublistView(p);
    out.add(_SnapshotChunk(
      view.getUint32(5, Endian.big),
      view.getUint32(9, Endian.big),
      view.getUint32(13, Endian.big),
      Uint8List.sublistView(p, 17),
    ));
  }
  return out;
}

/// Reassembled snapshot text per request id (offset-ordered concat).
Map<int, String> _snapshots(_GateHarness harness) {
  final byId = <int, List<_SnapshotChunk>>{};
  for (final c in _snapshotChunks(harness)) {
    byId.putIfAbsent(c.reqId, () => []).add(c);
  }
  return byId.map((id, chunks) {
    chunks.sort((a, b) => a.offset.compareTo(b.offset));
    final data = BytesBuilder(copy: false);
    for (final c in chunks) {
      data.add(c.data);
    }
    return MapEntry(id, utf8.decode(data.toBytes()));
  });
}
