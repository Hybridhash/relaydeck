import 'package:flet/flet.dart';
import 'package:flet/src/widgets/control_inherited_notifier.dart';
import 'package:flutter/cupertino.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

class _Backend extends FletBackend {
  _Backend()
      : super(
          pageUri: Uri.parse('http://localhost/'),
          assetsDir: '',
          multiView: false,
          extensions: const <FletExtension>[],
        );

  final events = <String>[];

  @override
  void triggerControlEventById(int controlId, String eventName,
      [dynamic eventData]) {
    events.add(eventName);
  }
}

const _text = 'stored passwords and codes are never touched';

Map<String, dynamic> _page(Map<String, dynamic> child) => {
      '_i': 1,
      '_c': 'Page',
      'content': {
        '_i': 10,
        '_c': 'Column',
        'controls': [child],
      },
    };

Future<_Backend> _mount(WidgetTester tester, Map<String, dynamic> child) async {
  final backend = _Backend();
  addTearDown(backend.dispose);
  backend.page.update(_page(child), shouldNotify: true);
  await tester.pumpWidget(ChangeNotifierProvider<FletBackend>.value(
    value: backend,
    child: MaterialApp(
      home: Scaffold(
        body: withControlInheritedNotifier(
          backend.page,
          (_) => backend.page.buildWidget('content')!,
        ),
      ),
    ),
  ));
  await tester.pumpAndSettle();
  return backend;
}

int _loupes() => find.byType(RawMagnifier).evaluate().length;

final _handles = find.descendant(
  of: find.byWidgetPredicate(
      (w) => w.runtimeType.toString() == '_SelectionHandleOverlay'),
  matching: find.byType(RawGestureDetector),
);

Future<void> _frames(WidgetTester t, [int n = 6]) async {
  for (var i = 0; i < n; i++) {
    await t.pump(const Duration(milliseconds: 50));
  }
}

/// Long-press at [at], keep the finger down and drag it. Returns the most
/// loupes seen while the finger was down.
Future<int> _longPressDrag(WidgetTester t, Offset at) async {
  final g = await t.startGesture(at);
  await t.pump(const Duration(milliseconds: 700));
  var most = _loupes();
  for (var i = 0; i < 6; i++) {
    await g.moveBy(const Offset(10, 0));
    await t.pump(const Duration(milliseconds: 16));
    most = most > _loupes() ? most : _loupes();
  }
  await _frames(t);
  most = most > _loupes() ? most : _loupes();
  await g.up();
  await _frames(t, 10);
  return most;
}

/// Drag the end handle of a finished selection.
Future<int> _handleDrag(WidgetTester t) async {
  final g = await t.startGesture(t.getCenter(_handles.last));
  var most = 0;
  for (var i = 0; i < 6; i++) {
    await g.moveBy(const Offset(12, 0));
    await t.pump(const Duration(milliseconds: 16));
    most = most > _loupes() ? most : _loupes();
  }
  await _frames(t);
  most = most > _loupes() ? most : _loupes();
  await g.up();
  await _frames(t, 10);
  return most;
}

void _ios(String name, Future<void> Function(WidgetTester) body) {
  testWidgets(name, (tester) async {
    debugDefaultTargetPlatformOverride = TargetPlatform.iOS;
    try {
      await body(tester);
    } finally {
      debugDefaultTargetPlatformOverride = null;
    }
  });
}

void main() {
  _ios('control: a stock SelectionArea does show the loupe on iOS', (t) async {
    await t.pumpWidget(const MaterialApp(
      home: Scaffold(
        body: Align(
          alignment: Alignment.topLeft,
          child: SelectionArea(child: Text(_text)),
        ),
      ),
    ));
    final g = await t.startGesture(const Offset(30, 10));
    await t.pump(const Duration(milliseconds: 700));
    await g.up();
    await _frames(t);
    expect(_handles, findsWidgets);
    expect(await _handleDrag(t), greaterThan(0));
  });

  _ios('SelectionArea control: handles and change event, never a loupe',
      (t) async {
    final backend = await _mount(t, {
      '_i': 20,
      '_c': 'SelectionArea',
      'on_change': true,
      'content': {'_i': 21, '_c': 'Text', 'value': _text},
    });
    final g = await t.startGesture(const Offset(30, 10));
    await t.pump(const Duration(milliseconds: 700));
    await g.up();
    await _frames(t);
    expect(_handles, findsWidgets);
    expect(backend.events, contains('change'));
    expect(await _handleDrag(t), 0);
    expect(_handles, findsWidgets);
  });

  _ios('TextField control: long-press drag and handles, never a loupe',
      (t) async {
    await _mount(t, {
      '_i': 30,
      '_c': 'TextField',
      'value': _text,
      'autofocus': true,
    });
    final field = find.byType(TextField);
    expect(field, findsOneWidget);
    final at = t.getTopLeft(field) + const Offset(40, 20);
    expect(await _longPressDrag(t, at), 0);
    final editable =
        t.state<EditableTextState>(find.byType(EditableText));
    expect(editable.widget.selectionControls, isNotNull);
    expect(editable.textEditingValue.selection.isValid, isTrue);
    expect(_loupes(), 0);
  });

  _ios('CupertinoTextField control: long-press drag, never a loupe',
      (t) async {
    await _mount(t, {
      '_i': 40,
      '_c': 'CupertinoTextField',
      'value': _text,
      'autofocus': true,
    });
    final field = find.byType(CupertinoTextField);
    expect(field, findsOneWidget);
    final at = t.getTopLeft(field) + const Offset(40, 20);
    expect(await _longPressDrag(t, at), 0);
    expect(_loupes(), 0);
  });
}
