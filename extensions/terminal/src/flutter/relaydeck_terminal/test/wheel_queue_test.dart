import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';

import '../lib/src/wheel_queue.dart';

// Real SGR wheel reports (button 64/65), kept byte-exact through the queue.
const _up = '\x1b[<65;80;24M';
const _down = '\x1b[<64;80;24M';

void main() {
  test('caps pending rows at 12', () {
    final sent = <String>[];
    final queue = WheelQueue(send: sent.add);
    queue.enqueue(true, 10, _up);
    queue.enqueue(true, 10, _up);
    expect(queue.pendingRows, 12);
    expect(sent, hasLength(3));
    queue.discard();
  });

  test('flushes 3 rows per flush until empty', () {
    final sent = <String>[];
    final queue = WheelQueue(send: sent.add);
    queue.enqueue(true, 10, _up);
    expect(sent, hasLength(3));
    queue.flush();
    expect(sent, hasLength(6));
    queue.flush();
    expect(sent, hasLength(9));
    queue.flush();
    expect(sent, hasLength(10));
    expect(queue.pendingRows, 0);
    queue.flush();
    expect(sent, hasLength(10));
  });

  test('direction change replaces the pending run', () {
    final sent = <String>[];
    final queue = WheelQueue(send: sent.add);
    queue.enqueue(true, 10, _up);
    queue.enqueue(false, 4, _down);
    expect(queue.pendingRows, 4);
    queue.flush();
    expect(sent, [_up, _up, _up, _down, _down, _down]);
    expect(queue.pendingRows, 1);
    queue.discard();
  });

  test('discard empties and stops the tick', () {
    FakeAsync().run((async) {
      final sent = <String>[];
      final queue = WheelQueue(send: sent.add);
      queue.enqueue(true, 10, _up);
      queue.discard();
      expect(queue.pendingRows, 0);
      async.elapse(const Duration(seconds: 1));
      expect(sent, hasLength(3));
    });
  });

  test('tick paces 3 rows per 33 ms until empty, then silent', () {
    FakeAsync().run((async) {
      final sent = <String>[];
      final queue = WheelQueue(send: sent.add);
      queue.enqueue(true, 10, _up);
      expect(sent, hasLength(3));
      async.elapse(const Duration(milliseconds: 33));
      expect(sent, hasLength(6));
      async.elapse(const Duration(milliseconds: 33));
      expect(sent, hasLength(9));
      async.elapse(const Duration(milliseconds: 33));
      expect(sent, hasLength(10));
      expect(queue.pendingRows, 0);
      async.elapse(const Duration(seconds: 1));
      expect(sent, hasLength(10));
    });
  });

  test('enqueue while ticking adds without an immediate flush', () {
    FakeAsync().run((async) {
      final sent = <String>[];
      final queue = WheelQueue(send: sent.add);
      queue.enqueue(true, 10, _up);
      queue.enqueue(true, 2, _up);
      expect(sent, hasLength(3));
      expect(queue.pendingRows, 9);
      async.elapse(const Duration(milliseconds: 33));
      expect(sent, hasLength(6));
    });
  });

  test('short run flushes at once with no trailing sends', () {
    FakeAsync().run((async) {
      final sent = <String>[];
      final queue = WheelQueue(send: sent.add);
      queue.enqueue(true, 2, _up);
      expect(sent, [_up, _up]);
      async.elapse(const Duration(seconds: 1));
      expect(sent, hasLength(2));
    });
  });

  test('separate enqueues within the tick pace instead of flushing', () {
    FakeAsync().run((async) {
      final sent = <String>[];
      final queue = WheelQueue(send: sent.add);
      queue.enqueue(true, 1, _up);
      expect(sent, hasLength(1));
      queue.enqueue(true, 1, _up);
      queue.enqueue(true, 1, _up);
      expect(sent, hasLength(1));
      expect(queue.pendingRows, 2);
      async.elapse(const Duration(milliseconds: 33));
      expect(sent, hasLength(3));
      async.elapse(const Duration(seconds: 1));
      expect(sent, hasLength(3));
    });
  });

  test('discard inside send keeps the count exact', () {
    FakeAsync().run((async) {
      final sent = <String>[];
      var fenced = false;
      late final WheelQueue queue;
      queue = WheelQueue(send: (report) {
        if (fenced) {
          queue.discard();
          return;
        }
        sent.add(report);
      });
      queue.enqueue(true, 10, _up);
      expect(sent, hasLength(3));
      expect(queue.pendingRows, 7);
      // A fence raised mid-drain retires the run: the tick sends
      // nothing, the debited rows are dropped, never negative.
      fenced = true;
      async.elapse(const Duration(milliseconds: 33));
      expect(sent, hasLength(3));
      expect(queue.pendingRows, 0);
      async.elapse(const Duration(seconds: 1));
      expect(sent, hasLength(3));
      fenced = false;
      queue.enqueue(true, 2, _up);
      expect(sent, hasLength(5));
      expect(queue.pendingRows, 0);
    });
  });

  test('zero rows enqueue nothing', () {
    final sent = <String>[];
    final queue = WheelQueue(send: sent.add);
    queue.enqueue(true, 0, _up);
    expect(sent, isEmpty);
    expect(queue.pendingRows, 0);
  });
}
