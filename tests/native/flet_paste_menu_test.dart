// Native half of the agent/shell paste-fallback contract. Python consumes the
// resulting selection/change events in
// tests/ui/terminal/test_compact_paste_fallback.py. Run from the
// terminal Flutter package with this file's absolute path and --no-pub.
// This test does not connect to a companion or claim Safari clipboard proof.
import 'dart:convert';
import 'dart:io';

import 'package:flet/flet.dart';
import 'package:flet/src/controls/textfield.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

class _CaptureBackend extends FletBackend {
  _CaptureBackend()
    : super(
        pageUri: Uri.parse('http://localhost/'),
        assetsDir: '',
        multiView: false,
        extensions: const <FletExtension>[],
      );

  final changes = <String>[];

  @override
  void triggerControlEvent(Control control, String name, [dynamic data]) {
    if (name == 'change' && control.hasEventHandler(name)) {
      changes.add(data as String);
    }
  }
}

void main() {
  final fixture =
      jsonDecode(
            File(
              '../../../../../tests/fixtures/typing/paste_menu.json',
            ).readAsStringSync(),
          )
          as Map<String, dynamic>;

  for (final name in (fixture['fields'] as List).cast<String>()) {
    testWidgets('$name: actual long-press Paste emits reviewed text contract', (
      tester,
    ) async {
      final paste = fixture['pasted_text'] as String;
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, (call) async {
            if (call.method == 'Clipboard.hasStrings') {
              return <String, Object>{'value': true};
            }
            if (call.method == 'Clipboard.getData') {
              return <String, Object>{'text': paste};
            }
            return null;
          });
      addTearDown(
        () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
            .setMockMethodCallHandler(SystemChannels.platform, null),
      );

      final backend = _CaptureBackend();
      addTearDown(backend.dispose);
      final control = Control(
        id: 1,
        type: 'TextField',
        properties: <String, dynamic>{
          ...fixture['field'] as Map<String, dynamic>,
          'key': name,
          'selection': <String, int>{'base_offset': 0, 'extent_offset': 0},
        },
        backend: backend,
      );
      await tester.pumpWidget(
        ChangeNotifierProvider<FletBackend>.value(
          value: backend,
          child: MaterialApp(
            theme: ThemeData(platform: TargetPlatform.iOS),
            home: Scaffold(
              body: Align(
                alignment: Alignment.bottomCenter,
                child: SizedBox(
                  width: 390,
                  height: 36,
                  child: TextFieldControl(control: control),
                ),
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      final field = find.byType(TextField);
      expect(
        tester.widget<TextField>(field).enableInteractiveSelection,
        isTrue,
      );
      await tester.tap(field);
      await tester.pumpAndSettle();
      await tester.longPress(field);
      await tester.pumpAndSettle();
      final pasteAction = find.text('Paste');
      expect(pasteAction, findsOneWidget);
      final editable = tester.state<EditableTextState>(
        find.byType(EditableText),
      );
      final before = editable.widget.controller.value;
      final expected = before.text.replaceRange(
        before.selection.start,
        before.selection.end,
        paste,
      );
      await tester.tap(pasteAction);
      await tester.pumpAndSettle();
      expect(editable.widget.controller.text, expected);
      expect(backend.changes, <String>[expected]);
      expect(expected.contains(paste), isTrue);
      await tester.pumpWidget(const SizedBox.shrink());
    });
  }
}
