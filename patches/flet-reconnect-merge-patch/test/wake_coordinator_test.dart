import 'dart:async';
import 'dart:typed_data';

import 'package:flet/flet.dart';
import 'package:flet/src/transport/flet_backend_channel.dart';
import 'package:flet/src/transport/flet_backend_channel_web_socket.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:msgpack_dart/msgpack_dart.dart' as msgpack;

class FakeChannel extends FletWebSocketBackendChannel {
  FakeChannel({
    required super.onDisconnect,
    required super.onPacket,
    required this.log,
  }) : super(address: 'http://fake/');

  final List<String> log;
  Completer<void>? connectGate;
  final sent = <Uint8List>[];
  var disconnects = 0;
  var local = false;

  @override
  bool get isLocalConnection => local;

  @override
  Future connect() async {
    log.add('connect');
    await connectGate?.future;
  }

  @override
  void send(Uint8List packet) {
    sent.add(packet);
  }

  @override
  void disconnect() {
    disconnects++;
  }

  void fireDone() => onDisconnect();
}

FletBackend makeBackend(
  List<FakeChannel> channels,
  List<String> log, {
  int? controlId,
  void Function(FakeChannel)? onChannel,
  bool disposeAfter = true,
}) {
  final backend = FletBackend(
    pageUri: Uri.parse('http://fake/'),
    assetsDir: '',
    multiView: false,
    extensions: const <FletExtension>[],
    controlId: controlId,
    channelBuilder: ({required onDisconnect, required onPacket}) {
      final fake = FakeChannel(
        onDisconnect: onDisconnect,
        onPacket: onPacket,
        log: log,
      );
      channels.add(fake);
      onChannel?.call(fake);
      return fake;
    },
  );
  if (disposeAfter) {
    addTearDown(backend.dispose);
  }
  return backend;
}

/// Captures debugPrint lines; restored after the test.
List<String> capturePrints() {
  final prints = <String>[];
  debugPrint = (String? message, {int? wrapWidth}) {
    prints.add(message ?? '');
  };
  addTearDown(() {
    debugPrint = debugPrintThrottled;
  });
  return prints;
}

/// A register-response packet setting the page route, as the server sends it.
Uint8List registerPacket(String route) {
  final body = msgpack.serialize([
    1,
    {
      'session_id': 'session-new',
      'page_patch': {'route': route},
      'error': null,
    },
  ]);
  final packet = Uint8List(1 + body.length);
  packet[0] = 0x00;
  packet.setRange(1, packet.length, body);
  return packet;
}

void main() {
  test('wake with no pending timer does nothing at startup', () {
    final prints = capturePrints();
    final channels = <FakeChannel>[];
    final backend = makeBackend(channels, <String>[]);
    backend.wake('visible');
    expect(channels, isEmpty);
    expect(prints.where((p) => p.contains('action=none')), hasLength(1));
  });

  test('wake while a socket is opening does nothing', () async {
    final prints = capturePrints();
    final channels = <FakeChannel>[];
    final gate = Completer<void>();
    final backend = makeBackend(
      channels,
      <String>[],
      onChannel: (c) => c.connectGate = gate,
    );
    final connecting = backend.connect();
    await Future.delayed(Duration.zero);
    backend.wake('visible');
    expect(prints.where((p) => p.contains('action=none')), hasLength(1));
    gate.complete();
    await connecting;
    expect(channels.length, 1);
  });

  test('wake on a live socket does nothing', () async {
    final prints = capturePrints();
    final channels = <FakeChannel>[];
    final log = <String>[];
    final backend = makeBackend(channels, log);
    await backend.connect();
    channels[0].onPacket(registerPacket('/live'));
    expect(backend.isLoading, isFalse);
    backend.wake('visible');
    expect(log, ['connect']);
    expect(channels.length, 1);
    expect(prints.where((p) => p.contains('action=none')), hasLength(1));
  });

  test('disconnect then wake connects now and exactly once', () async {
    final prints = capturePrints();
    final channels = <FakeChannel>[];
    final backend = makeBackend(channels, <String>[]);
    await backend.connect();
    channels[0].fireDone();
    backend.wake('visible');
    expect(channels.length, 2);
    expect(prints.where((p) => p.contains('action=retry_now')), hasLength(1));
    await Future.delayed(const Duration(milliseconds: 600));
    expect(channels.length, 2);
  });

  test('second wake right after does nothing', () async {
    final prints = capturePrints();
    final channels = <FakeChannel>[];
    final backend = makeBackend(channels, <String>[]);
    await backend.connect();
    channels[0].fireDone();
    backend.wake('visible');
    backend.wake('online');
    expect(channels.length, 2);
    expect(prints.where((p) => p.contains('action=retry_now')), hasLength(1));
    expect(prints.where((p) => p.contains('action=none')), hasLength(1));
  });

  test('wake after the timer fired does nothing', () async {
    final prints = capturePrints();
    final channels = <FakeChannel>[];
    final backend = makeBackend(channels, <String>[]);
    await backend.connect();
    channels[0].fireDone();
    await Future.delayed(const Duration(milliseconds: 600));
    expect(channels.length, 2);
    backend.wake('visible');
    expect(channels.length, 2);
    expect(prints.where((p) => p.contains('action=none')), hasLength(1));
  });

  test('stale generation onDone is ignored', () async {
    final channels = <FakeChannel>[];
    final backend = makeBackend(channels, <String>[]);
    await backend.connect();
    channels[0].fireDone();
    backend.wake('visible');
    expect(channels.length, 2);
    channels[0].fireDone();
    await Future.delayed(const Duration(milliseconds: 600));
    expect(channels.length, 2);
  });

  test('stale packet from a retired generation is ignored', () async {
    final channels = <FakeChannel>[];
    final backend = makeBackend(channels, <String>[]);
    await backend.connect();
    channels[0].fireDone();
    backend.wake('visible');
    expect(channels.length, 2);
    channels[0].onPacket(registerPacket('/stale'));
    expect(backend.isLoading, isTrue);
    expect(backend.page.get('route'), isNull);
    channels[1].onPacket(registerPacket('/live'));
    expect(backend.isLoading, isFalse);
    expect(backend.page.get('route'), '/live');
  });

  test('dispose during connect disconnects without registering', () async {
    final channels = <FakeChannel>[];
    final gate = Completer<void>();
    final backend = makeBackend(
      channels,
      <String>[],
      onChannel: (c) => c.connectGate = gate,
      disposeAfter: false,
    );
    final connecting = backend.connect();
    await Future.delayed(Duration.zero);
    backend.dispose();
    gate.complete();
    await connecting;
    expect(channels.single.disconnects, greaterThanOrEqualTo(1));
    expect(channels.single.sent, isEmpty);
  });

  test('embedded backend retry is unchanged', () async {
    final channels = <FakeChannel>[];
    final backend = makeBackend(channels, <String>[], controlId: 1);
    await backend.connect();
    channels[0].fireDone();
    await Future.delayed(const Duration(milliseconds: 600));
    expect(channels.length, 2);
  });

  test('remote retry uses the policy delay', () async {
    final channels = <FakeChannel>[];
    final backend = makeBackend(channels, <String>[]);
    await backend.connect();
    channels[0].fireDone();
    await Future.delayed(const Duration(milliseconds: 200));
    expect(channels.length, 1);
    await Future.delayed(const Duration(milliseconds: 400));
    expect(channels.length, 2);
  });

  test(
    'wake during a suspended connect still retries after its disconnect',
    () async {
      final channels = <FakeChannel>[];
      final gate = Completer<void>();
      final backend = makeBackend(
        channels,
        <String>[],
        onChannel: (c) => c.connectGate = gate,
      );
      final first = backend.connect();
      await Future.delayed(Duration.zero);
      channels[0].fireDone();
      backend.wake('visible');
      expect(channels.length, 2);
      gate.complete();
      await first;
      await Future.delayed(Duration.zero);
      expect(channels.length, 2);
      expect(channels[0].sent, isEmpty);
      expect(channels[1].sent, isNotEmpty);
    },
  );

  test('timer expiring during a suspended connect still retries', () async {
    final channels = <FakeChannel>[];
    final gate = Completer<void>();
    final backend = makeBackend(
      channels,
      <String>[],
      onChannel: (c) => c.connectGate = gate,
    );
    final first = backend.connect();
    await Future.delayed(Duration.zero);
    channels[0].fireDone();
    await Future.delayed(const Duration(milliseconds: 600));
    expect(channels.length, 2);
    gate.complete();
    await first;
    await Future.delayed(Duration.zero);
    expect(channels[1].sent, isNotEmpty);
    expect(channels[0].sent, isEmpty);
  });

  test('older attempt settling first disconnects only itself', () async {
    final channels = <FakeChannel>[];
    final gates = <Completer<void>>[];
    final backend = makeBackend(
      channels,
      <String>[],
      onChannel: (c) {
        final gate = Completer<void>();
        c.connectGate = gate;
        gates.add(gate);
      },
    );
    final a = backend.connect();
    await Future.delayed(Duration.zero);
    final b = backend.connect();
    await Future.delayed(Duration.zero);
    expect(channels.length, 2);
    gates[0].complete();
    await a;
    await Future.delayed(Duration.zero);
    expect(channels[0].disconnects, 1);
    expect(channels[0].sent, isEmpty);
    expect(channels[1].disconnects, 0);
    gates[1].complete();
    await b;
    await Future.delayed(Duration.zero);
    expect(channels[1].sent, isNotEmpty);
    expect(channels[1].disconnects, 0);
  });

  test('newer attempt settling first is unaffected by the older', () async {
    final channels = <FakeChannel>[];
    final gates = <Completer<void>>[];
    final backend = makeBackend(
      channels,
      <String>[],
      onChannel: (c) {
        final gate = Completer<void>();
        c.connectGate = gate;
        gates.add(gate);
      },
    );
    final a = backend.connect();
    await Future.delayed(Duration.zero);
    final b = backend.connect();
    await Future.delayed(Duration.zero);
    gates[1].complete();
    await b;
    await Future.delayed(Duration.zero);
    expect(channels[1].sent, isNotEmpty);
    expect(channels[1].disconnects, 0);
    expect(channels[0].disconnects, 0);
    gates[0].complete();
    await a;
    await Future.delayed(Duration.zero);
    expect(channels[0].disconnects, 1);
    expect(channels[0].sent, isEmpty);
    expect(channels[1].disconnects, 0);
  });

  test(
    'retired generic failure before the winner succeeds changes nothing',
    () async {
      final channels = <FakeChannel>[];
      final gates = <Completer<void>>[];
      final backend = makeBackend(
        channels,
        <String>[],
        onChannel: (c) {
          final gate = Completer<void>();
          c.connectGate = gate;
          gates.add(gate);
        },
      );
      var notifies = 0;
      backend.addListener(() => notifies++);
      final a = backend.connect();
      await Future.delayed(Duration.zero);
      channels[0].fireDone();
      backend.wake('visible');
      expect(channels.length, 2);
      final settledNotifies = notifies;
      gates[0].completeError(Exception('boom'));
      await a;
      await Future.delayed(Duration.zero);
      expect(channels[0].disconnects, 1);
      expect(channels[0].sent, isEmpty);
      expect(channels[1].disconnects, 0);
      expect(backend.error, '');
      expect(backend.isLoading, isTrue);
      expect(notifies, settledNotifies);
      gates[1].complete();
      await Future.delayed(Duration.zero);
      expect(channels[1].sent, isNotEmpty);
      expect(backend.error, '');
    },
  );

  test(
    'retired generic failure after the winner succeeds changes nothing',
    () async {
      final channels = <FakeChannel>[];
      final gates = <Completer<void>>[];
      final backend = makeBackend(
        channels,
        <String>[],
        onChannel: (c) {
          final gate = Completer<void>();
          c.connectGate = gate;
          gates.add(gate);
        },
      );
      var notifies = 0;
      backend.addListener(() => notifies++);
      final a = backend.connect();
      await Future.delayed(Duration.zero);
      channels[0].fireDone();
      backend.wake('visible');
      gates[1].complete();
      await Future.delayed(Duration.zero);
      channels[1].onPacket(registerPacket('/live'));
      expect(backend.isLoading, isFalse);
      final settledNotifies = notifies;
      gates[0].completeError(Exception('boom'));
      await a;
      await Future.delayed(Duration.zero);
      expect(channels[0].disconnects, 1);
      expect(channels[0].sent, isEmpty);
      expect(channels[1].disconnects, 0);
      expect(backend.error, '');
      expect(backend.isLoading, isFalse);
      expect(notifies, settledNotifies);
    },
  );

  test(
    'retired startup failure before the winner succeeds changes nothing',
    () async {
      final channels = <FakeChannel>[];
      final gates = <Completer<void>>[];
      final backend = makeBackend(
        channels,
        <String>[],
        onChannel: (c) {
          final gate = Completer<void>();
          c.connectGate = gate;
          gates.add(gate);
        },
      );
      var notifies = 0;
      backend.addListener(() => notifies++);
      final a = backend.connect();
      await Future.delayed(Duration.zero);
      channels[0].fireDone();
      backend.wake('visible');
      expect(channels.length, 2);
      final settledNotifies = notifies;
      gates[0].completeError(const FletAppStartupException('boom'));
      await a;
      await Future.delayed(Duration.zero);
      expect(channels[0].disconnects, 1);
      expect(channels[0].sent, isEmpty);
      expect(channels[1].disconnects, 0);
      expect(backend.error, '');
      expect(backend.isLoading, isTrue);
      expect(notifies, settledNotifies);
      gates[1].complete();
      await Future.delayed(Duration.zero);
      expect(channels[1].sent, isNotEmpty);
      expect(backend.error, '');
    },
  );

  test(
    'retired startup failure after the winner succeeds changes nothing',
    () async {
      final channels = <FakeChannel>[];
      final gates = <Completer<void>>[];
      final backend = makeBackend(
        channels,
        <String>[],
        onChannel: (c) {
          final gate = Completer<void>();
          c.connectGate = gate;
          gates.add(gate);
        },
      );
      var notifies = 0;
      backend.addListener(() => notifies++);
      final a = backend.connect();
      await Future.delayed(Duration.zero);
      channels[0].fireDone();
      backend.wake('visible');
      gates[1].complete();
      await Future.delayed(Duration.zero);
      channels[1].onPacket(registerPacket('/live'));
      expect(backend.isLoading, isFalse);
      final settledNotifies = notifies;
      gates[0].completeError(const FletAppStartupException('boom'));
      await a;
      await Future.delayed(Duration.zero);
      expect(channels[0].disconnects, 1);
      expect(channels[0].sent, isEmpty);
      expect(channels[1].disconnects, 0);
      expect(backend.error, '');
      expect(backend.isLoading, isFalse);
      expect(notifies, settledNotifies);
    },
  );

  test('disposal then generic failure changes nothing', () async {
    final channels = <FakeChannel>[];
    final gate = Completer<void>();
    final backend = makeBackend(
      channels,
      <String>[],
      onChannel: (c) => c.connectGate = gate,
      disposeAfter: false,
    );
    var notifies = 0;
    backend.addListener(() => notifies++);
    final connecting = backend.connect();
    await Future.delayed(Duration.zero);
    backend.dispose();
    gate.completeError(Exception('boom'));
    await connecting;
    await Future.delayed(Duration.zero);
    expect(backend.error, '');
    expect(notifies, 0);
    expect(channels.single.disconnects, 2);
  });

  test('disposal then startup failure changes nothing', () async {
    final channels = <FakeChannel>[];
    final gate = Completer<void>();
    final backend = makeBackend(
      channels,
      <String>[],
      onChannel: (c) => c.connectGate = gate,
      disposeAfter: false,
    );
    var notifies = 0;
    backend.addListener(() => notifies++);
    final connecting = backend.connect();
    await Future.delayed(Duration.zero);
    backend.dispose();
    gate.completeError(const FletAppStartupException('boom'));
    await connecting;
    await Future.delayed(Duration.zero);
    expect(backend.error, '');
    expect(notifies, 0);
    expect(channels.single.disconnects, 2);
  });

  test('current-generation connect failure still schedules a retry', () async {
    final channels = <FakeChannel>[];
    final gate = Completer<void>();
    final backend = makeBackend(
      channels,
      <String>[],
      onChannel: (c) => c.connectGate = gate,
    );
    final connecting = backend.connect();
    await Future.delayed(Duration.zero);
    gate.completeError(Exception('boom'));
    await connecting;
    expect(backend.error, contains('boom'));
    await Future.delayed(const Duration(milliseconds: 600));
    expect(channels.length, 2);
  });

  test('local retry uses the fixed delay', () async {
    final channels = <FakeChannel>[];
    final backend = makeBackend(
      channels,
      <String>[],
      onChannel: (c) => c.local = true,
    );
    await backend.connect();
    channels[0].fireDone();
    await Future.delayed(const Duration(milliseconds: 450));
    expect(channels.length, 1);
    await Future.delayed(const Duration(milliseconds: 750));
    expect(channels.length, 2);
  });
}
