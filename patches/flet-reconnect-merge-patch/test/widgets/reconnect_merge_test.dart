import 'package:flet/flet.dart';
import 'package:flet/src/widgets/control_inherited_notifier.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

class _RecordingBackend extends FletBackend {
  _RecordingBackend()
      : super(
          pageUri: Uri.parse('http://localhost/'),
          assetsDir: '',
          multiView: false,
          extensions: const <FletExtension>[],
        );

  final events = <Map<String, dynamic>>[];

  @override
  void triggerControlEventById(
    int controlId,
    String eventName, [
    dynamic eventData,
  ]) {
    events.add({'target': controlId, 'name': eventName, 'data': eventData});
  }
}

Map<String, dynamic> _snapshot() => {
      '_i': 1,
      '_c': 'Page',
      'content': {
        '_i': 10,
        '_c': 'Column',
        'controls': [
          {
            '_i': 11,
            '_c': 'Row',
            'controls': [
              {
                '_i': 12,
                '_c': 'IconButton',
                'disabled': true,
                'on_click': true,
                'content': {'_i': 13, '_c': 'Text', 'value': 'Send'},
              },
            ],
          },
        ],
      },
    };

Map<String, dynamic> _button(int id) => {
      '_i': id,
      '_c': 'IconButton',
      'disabled': true,
      'on_click': true,
      'content': {'_i': id + 100, '_c': 'Text', 'value': 'Send $id'},
    };

Map<String, dynamic> _buttons(List<int> ids) => {
      '_i': 1,
      '_c': 'Page',
      'content': {
        '_i': 10,
        '_c': 'Row',
        'controls': ids.map(_button).toList(),
      },
    };

Future<_RecordingBackend> _mount(
    WidgetTester tester, Map<String, dynamic> snapshot) async {
  final backend = _RecordingBackend();
  addTearDown(backend.dispose);
  backend.page.update(snapshot, shouldNotify: true);
  await tester.pumpWidget(ChangeNotifierProvider<FletBackend>.value(
    value: backend,
    child: MaterialApp(
      home: Scaffold(
        body: withControlInheritedNotifier(
          backend.page,
          (_) => Column(children: [
            if (backend.page.child('content') != null)
              backend.page.buildWidget('content')!,
            ...backend.page.buildWidgets('overlay'),
          ]),
        ),
      ),
    ),
  ));
  await tester.pumpAndSettle();
  return backend;
}

Future<void> _enableAndTap(
    WidgetTester tester, _RecordingBackend backend, int id) async {
  backend.controlsIndex.get(id)!.applyPatch([
    [0],
    [0, 0, 'disabled', false],
  ], backend);
  await tester.pumpAndSettle();
  await tester.tap(find.widgetWithText(IconButton, 'Send $id'));
  await tester.pump();
}

void main() {
  testWidgets('reconnect snapshot preserves the live Send notifier', (
    tester,
  ) async {
    final backend = _RecordingBackend();
    addTearDown(backend.dispose);
    final page = backend.page;
    page.update(_snapshot(), shouldNotify: true);
    final retained = {
      for (final id in [10, 11, 12, 13]) id: backend.controlsIndex.get(id)!,
    };
    await tester.pumpWidget(
      ChangeNotifierProvider<FletBackend>.value(
        value: backend,
        child: MaterialApp(
          home: Scaffold(
            body: withControlInheritedNotifier(
              page,
              (_) => page.buildWidget('content')!,
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(
      tester.widget<IconButton>(find.byType(IconButton)).onPressed,
      isNull,
    );

    // The register-client response invokes this real full-page merge.
    page.update(_snapshot(), shouldNotify: true);
    await tester.pumpAndSettle();
    // Python's disabled -> enabled replace operation, routed to the button.
    page.applyPatch([
      [
        0,
        {
          'content': [
            2,
            {
              'controls': [
                3,
                {
                  0: [
                    4,
                    {
                      'controls': [
                        5,
                        {
                          0: [1],
                        },
                      ],
                    },
                  ],
                },
              ],
            },
          ],
        },
      ],
      [0, 1, 'disabled', false],
    ], backend);
    await tester.pumpAndSettle();
    await tester.tap(find.byType(IconButton));
    await tester.pump();
    expect(backend.events.where((e) => e['name'] == 'click').toList(), [
      {'target': 12, 'name': 'click', 'data': null},
    ]);
    for (final entry in retained.entries) {
      expect(
        identical(backend.controlsIndex.get(entry.key), entry.value),
        isTrue,
      );
    }
  });

  testWidgets('composer Send suffix survives reconnect and later enabled patch',
      (tester) async {
    Map<String, dynamic> snapshot() => {
          '_i': 1,
          '_c': 'Page',
          'content': {
            '_i': 10,
            '_c': 'Column',
            'controls': [
              {
                '_i': 11,
                '_c': 'TextField',
                'value': 'exact unsent draft',
                'suffix_icon': _button(20),
              },
            ],
          },
        };
    final backend = await _mount(tester, snapshot());
    final field = backend.controlsIndex.get(11)!;
    final send = backend.controlsIndex.get(20)!;
    backend.page.update(snapshot(), shouldNotify: true);
    await _enableAndTap(tester, backend, 20);
    expect(backend.events.where((e) => e['name'] == 'click').length, 1);
    expect(identical(backend.controlsIndex.get(11), field), isTrue);
    expect(identical(backend.controlsIndex.get(20), send), isTrue);
    expect(tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'exact unsent draft');
  });

  test('identical matching lists keep identity without notifying', () {
    final backend = _RecordingBackend();
    addTearDown(backend.dispose);
    final page = backend.page;
    page.update(_snapshot());
    final button = backend.controlsIndex.get(12)!;
    var notifications = 0;
    button.addListener(() => notifications++);
    expect(page.update(_snapshot(), shouldNotify: true), isFalse);
    expect(identical(backend.controlsIndex.get(12), button), isTrue);
    expect(notifications, 0);
  });

  for (final mismatch in [
    'id',
    'control-to-data',
    'data-to-control',
  ]) {
    test('$mismatch mismatch replaces the list as before', () {
      final backend = _RecordingBackend();
      addTearDown(backend.dispose);
      final page = backend.page;
      final original = <dynamic>[
        mismatch == 'data-to-control'
            ? 'plain'
            : {'_i': 20, '_c': 'Text', 'value': 'old'},
      ];
      page.update({'items': original});
      final retained = page.get('items') as List;
      final incoming = <dynamic>[
        mismatch == 'control-to-data'
            ? 'plain'
            : {'_i': mismatch == 'id' ? 21 : 20, '_c': 'Text', 'value': 'new'},
      ];
      expect(page.update({'items': incoming}), isTrue);
      final replaced = page.get('items') as List;
      expect(identical(replaced, retained), isFalse);
      expect(replaced.length, incoming.length);
      if (replaced.first is Control) {
        expect(identical(replaced.first, retained.first), isFalse);
      }
    });
  }

  test('matching mixed lists merge controls and replace ordinary data', () {
    final backend = _RecordingBackend();
    addTearDown(backend.dispose);
    final page = backend.page;
    page.update({
      'items': [
        {'_i': 20, '_c': 'Text', 'value': 'old'},
        'old',
        {'plain': 'old'},
      ],
    });
    final retained = page.get('items') as List;
    final child = retained.first as Control;
    var parentNotifications = 0, childNotifications = 0;
    page.addListener(() => parentNotifications++);
    child.addListener(() => childNotifications++);
    expect(
      page.update({
        'items': [
          {'_i': 20, '_c': 'Text', 'value': 'new'},
          'new',
          {'plain': 'new'},
        ],
      }, shouldNotify: true),
      isTrue,
    );
    final updated = page.get('items') as List;
    expect(identical(updated.first, child), isTrue);
    expect(child.get('value'), 'new');
    expect(updated[1], 'new');
    expect(updated[2], {'plain': 'new'});
    expect(parentNotifications, 1);
    expect(childNotifications, 1);
  });

  test('partial update callers retain properties omitted from their maps', () {
    final backend = _RecordingBackend();
    addTearDown(backend.dispose);
    final page = backend.page;
    page.update(_snapshot());
    final button = backend.controlsIndex.get(12)!;
    page.update({'route': '/agents'});
    backend.updateControl(12, {'tooltip': 'Send'}, python: false);
    final window = page.child('window')!;
    page.update({
      'window': {'_i': window.id, 'width': 390}
    });
    expect(identical(page.child('window'), window), isTrue);
    expect(window.get('width'), 390);
    expect(button.disabled, isTrue);
    expect(button.get('on_click'), isTrue);
    expect(identical(backend.controlsIndex.get(12), button), isTrue);
  });

  for (final ids in [
    <int>[30, 20],
    <int>[15, 20, 30]
  ]) {
    testWidgets('away reorder or insertion $ids keeps live button notifiers',
        (tester) async {
      final backend = await _mount(tester, _buttons([20, 30]));
      final old20 = backend.controlsIndex.get(20)!;
      final old30 = backend.controlsIndex.get(30)!;
      backend.page.update(_buttons(ids), shouldNotify: true);
      await tester.pumpAndSettle();
      for (final id in ids) {
        await _enableAndTap(tester, backend, id);
      }
      expect(
          backend.events
              .where((e) => e['name'] == 'click')
              .map((e) => e['target']),
          ids);
      expect(identical(backend.controlsIndex.get(20), old20), isTrue);
      expect(identical(backend.controlsIndex.get(30), old30), isTrue);
      final row = backend.page.child('content')!;
      expect(row.children('controls').map((c) => c.id), ids);
    });
  }

  testWidgets('away visibility change rebuilds the owner for a retained child',
      (tester) async {
    final initial = _buttons([20]);
    final row = initial['content'] as Map;
    (row['controls'] as List).single['visible'] = false;
    final backend = await _mount(tester, initial);
    final button = backend.controlsIndex.get(20)!;
    expect(find.text('Send 20'), findsNothing);
    final restored = _buttons([20]);
    ((restored['content'] as Map)['controls'] as List).single['visible'] = true;
    backend.page.update(restored, shouldNotify: true);
    await tester.pumpAndSettle();
    await _enableAndTap(tester, backend, 20);
    expect(backend.events.where((e) => e['name'] == 'click').length, 1);
    expect(identical(backend.controlsIndex.get(20), button), isTrue);
  });

  testWidgets('away removal retires index entries and stale callbacks',
      (tester) async {
    final backend = await _mount(tester, _buttons([20, 30]));
    final removed = backend.controlsIndex.get(20)!;
    final survivor = backend.controlsIndex.get(30)!;
    backend.page.update(_buttons([30]), shouldNotify: true);
    await tester.pumpAndSettle();
    expect(backend.controlsIndex.get(20), isNull);
    expect(backend.controlsIndex.get(120), isNull);
    expect(find.text('Send 20'), findsNothing);
    removed.triggerEvent('click');
    removed.triggerEventWithoutSubscribers('click');
    expect(backend.events, isEmpty);
    await _enableAndTap(tester, backend, 30);
    expect(
        backend.events
            .where((e) => e['name'] == 'click')
            .map((e) => e['target']),
        [30]);
    expect(identical(backend.controlsIndex.get(30), survivor), isTrue);
  });

  testWidgets(
      'dialog overlay button keeps its mounted notifier after reconnect',
      (tester) async {
    Map<String, dynamic> snapshot() => {
          '_i': 1,
          '_c': 'Page',
          'overlay': [
            {
              '_i': 40,
              '_c': 'AlertDialog',
              'open': true,
              'content': {
                '_i': 41,
                '_c': 'Column',
                'tight': true,
                'controls': [_button(20)],
              },
            },
          ],
        };
    final backend = await _mount(tester, snapshot());
    final button = backend.controlsIndex.get(20)!;
    backend.page.update(snapshot(), shouldNotify: true);
    await tester.pumpAndSettle();
    expect(find.byType(AlertDialog), findsOneWidget);
    await _enableAndTap(tester, backend, 20);
    expect(backend.events.where((e) => e['name'] == 'click').length, 1);
    expect(identical(backend.controlsIndex.get(20), button), isTrue);
    // Close the route before provider/backend teardown.
    backend.updateControl(40, {'open': false}, python: false, notify: true);
    await tester.pumpAndSettle();
  });

  testWidgets('component body preserves identity and parent notifications',
      (tester) async {
    Map<String, dynamic> snapshot() => {
          '_i': 1,
          '_c': 'Page',
          'content': {
            '_i': 10,
            '_c': 'Row',
            'controls': [
              {'_i': 40, '_c': 'C', '_b': _button(20)},
            ],
          },
        };
    final backend = await _mount(tester, snapshot());
    final component = backend.controlsIndex.get(40)!;
    final button = backend.controlsIndex.get(20)!;
    backend.page.update(snapshot(), shouldNotify: true);
    await _enableAndTap(tester, backend, 20);
    expect(backend.events.where((e) => e['name'] == 'click').length, 1);
    expect(component.notifyParent, isTrue);
    expect(identical(backend.controlsIndex.get(40), component), isTrue);
    expect(identical(backend.controlsIndex.get(20), button), isTrue);
  });

  test('maps and nested lists reconcile moved controls and fix ancestry', () {
    final backend = _RecordingBackend();
    addTearDown(backend.dispose);
    backend.page.update(_buttons([20, 30]));
    final button20 = backend.controlsIndex.get(20)!;
    final button30 = backend.controlsIndex.get(30)!;
    backend.page.update({
      'content': {
        '_i': 50,
        '_c': 'Column',
        'disabled': true,
        'pockets': [
          {
            'nested': [
              [_button(30), _button(20)..['disabled'] = false]
            ]
          },
        ],
      },
    }, shouldNotify: true);
    expect(backend.controlsIndex.get(10), isNull);
    expect(identical(backend.controlsIndex.get(20), button20), isTrue);
    expect(identical(backend.controlsIndex.get(30), button30), isTrue);
    expect(button20.parent?.id, 50);
    expect(button20.get('disabled'), isFalse);
    expect(button20.disabled, isTrue);
    final nested = ((backend.page.child('content')!.get('pockets') as List)
        .single as Map)['nested'] as List;
    expect((nested.single as List).map((c) => (c as Control).id), [30, 20]);
  });

  test(
      'type mismatch replaces and retires the old instance without removing its successor',
      () {
    final backend = _RecordingBackend();
    addTearDown(backend.dispose);
    backend.page.update(_buttons([20]));
    final old = backend.controlsIndex.get(20)!;
    backend.page.update({
      'content': {
        '_i': 10,
        '_c': 'Row',
        'controls': [
          {'_i': 20, '_c': 'Text', 'value': 'replacement'}
        ],
      },
    });
    final replacement = backend.controlsIndex.get(20)!;
    expect(replacement.type, 'Text');
    expect(identical(replacement, old), isFalse);
    expect(backend.controlsIndex.get(120), isNull);
    old.triggerEvent('click');
    expect(backend.events, isEmpty);
  });

  test(
      'explicit empty overlays retire children without clearing unrelated props',
      () {
    final backend = _RecordingBackend();
    addTearDown(backend.dispose);
    backend.page.update({
      'overlay': [_button(20)],
      'route': '/kept'
    });
    backend.page.update({'overlay': []}, shouldNotify: true);
    expect(backend.page.get('overlay'), isEmpty);
    expect(backend.controlsIndex.get(20), isNull);
    expect(backend.controlsIndex.get(120), isNull);
    expect(backend.page.get('route'), '/kept');
  });
}
