// Pasting into the focused Flutter text field from the paste event's text: the
// insertion EditableText's own paste performs, without a clipboard read.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import '../lib/src/field_paste.dart';

Future<TextEditingController> pump(WidgetTester tester,
    {String text = '',
    bool readOnly = false,
    bool multiline = true,
    List<String>? changes}) async {
  final controller = TextEditingController(text: text);
  final node = FocusNode();
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Column(children: [
        TextField(
          controller: controller,
          focusNode: node,
          readOnly: readOnly,
          maxLines: multiline ? null : 1,
          onChanged: changes?.add,
        ),
        const TextButton(onPressed: null, child: Text('other')),
      ]),
    ),
  ));
  node.requestFocus();
  await tester.pump();
  return controller;
}

void main() {
  testWidgets('text lands at the caret and the caret follows it',
      (tester) async {
    final changes = <String>[];
    final controller = await pump(tester, text: 'ab', changes: changes);
    controller.selection = const TextSelection.collapsed(offset: 1);
    await tester.pump();
    final field = focusedEditable();
    expect(field, isNotNull);
    expect(pasteIntoField(field!, 'XY'), isTrue);
    await tester.pump();
    expect(controller.text, 'aXYb');
    expect(controller.selection, const TextSelection.collapsed(offset: 3));
    expect(changes, ['aXYb']);
  });

  testWidgets('a selection is replaced, once', (tester) async {
    final changes = <String>[];
    final controller = await pump(tester, text: 'hello world', changes: changes);
    controller.selection =
        const TextSelection(baseOffset: 6, extentOffset: 11);
    await tester.pump();
    expect(pasteIntoField(focusedEditable()!, 'there\nagain'), isTrue);
    await tester.pump();
    expect(controller.text, 'hello there\nagain');
    expect(changes.length, 1);
  });

  testWidgets("a single-line field's own formatter still runs", (tester) async {
    final controller = await pump(tester, text: '', multiline: false);
    expect(pasteIntoField(focusedEditable()!, 'one\ntwo'), isTrue);
    await tester.pump();
    expect(controller.text, 'onetwo');
  });

  testWidgets('a read-only field takes nothing', (tester) async {
    final controller = await pump(tester, text: 'ro', readOnly: true);
    expect(pasteIntoField(focusedEditable()!, 'x'), isFalse);
    expect(controller.text, 'ro');
  });

  testWidgets('no text field in focus means no field', (tester) async {
    await pump(tester);
    FocusManager.instance.primaryFocus?.unfocus();
    await tester.pump();
    expect(focusedEditable(), isNull);
  });
}
