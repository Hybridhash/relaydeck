// Wheel/trackpad line accounting: direction and packets at the half-line and
// fractional thresholds. Pure Dart, mirrors what the idle flush sends.
import 'package:flutter_test/flutter_test.dart';

import '../lib/src/swipe_math.dart';

const double line = 20;

/// Replays wheel deltas through idle flushes, as the terminal does, and returns
/// the emitted packets as signed line counts (positive: toward latest).
List<int> replay(List<double> deltas) {
  var run = 0.0;
  final packets = <int>[];
  for (final delta in deltas) {
    if (run != 0 && delta.sign != run.sign) {
      // A real direction flip commits the pending run and drops its remainder.
      final flushed = swipeFlushAccount(run, line, keepRemainder: false);
      if (flushed.lines > 0) packets.add(run.sign.toInt() * flushed.lines);
      run = 0;
    }
    run += delta;
    final flush = swipeFlushAccount(run, line, keepRemainder: true);
    if (flush.lines > 0) packets.add(run.sign.toInt() * flush.lines);
    run = flush.remainder;
  }
  return packets;
}

void main() {
  group('swipeFlushAccount', () {
    test('below a line nothing is emitted and the run is kept', () {
      final flush = swipeFlushAccount(9.9, line, keepRemainder: true);
      expect(flush.lines, 0);
      expect(flush.remainder, 9.9);
    });

    test('without keepRemainder a micro-touch is dropped', () {
      final flush = swipeFlushAccount(5, line, keepRemainder: false);
      expect(flush.lines, 0);
      expect(flush.remainder, 0);
    });

    test('exactly a half line waits for the rest of the line', () {
      final flush = swipeFlushAccount(10, line, keepRemainder: true);
      expect(flush.lines, 0);
      expect(flush.remainder, 10);
    });

    test('a one-shot run still rounds, as touch gestures always did', () {
      expect(swipeFlushAccount(10, line, keepRemainder: false).lines, 1);
      expect(swipeFlushAccount(9.9, line, keepRemainder: false).lines, 0);
    });

    test('the retained remainder never changes sign or exceeds a line', () {
      for (final pixels in [10.0, 14.9, 19.9, 20.0, 29.9, 30.0, 39.9, 71.0]) {
        for (final sign in [1.0, -1.0]) {
          final flush =
              swipeFlushAccount(pixels * sign, line, keepRemainder: true);
          expect(flush.remainder * sign >= 0, isTrue,
              reason: '$pixels flush left ${flush.remainder}');
          expect(flush.remainder.abs() < line, isTrue);
        }
      }
    });

    test('fractional thresholds emit whole lines only', () {
      final under = swipeFlushAccount(19.9, line, keepRemainder: true);
      expect((under.lines, under.remainder), (0, 19.9));
      final exact = swipeFlushAccount(20.0, line, keepRemainder: true);
      expect((exact.lines, exact.remainder), (1, 0.0));
      final over = swipeFlushAccount(29.9, line, keepRemainder: true);
      expect(over.lines, 1);
      expect(over.remainder, closeTo(9.9, 1e-9));
      final two = swipeFlushAccount(-40.5, line, keepRemainder: true);
      expect(two.lines, 2);
      expect(two.remainder, closeTo(-0.5, 1e-9));
    });
  });

  group('wheel replay', () {
    test('+10 then +5 sends nothing, and never a reverse swipe', () {
      expect(replay([10, 5]), isEmpty);
    });

    test('+10 then +10 sends exactly one swipe toward latest', () {
      expect(replay([10, 10]), [1]);
    });

    test('-10 then -10 mirrors it', () {
      expect(replay([-10, -10]), [-1]);
    });

    test('slow same-direction steps accumulate, one packet per line', () {
      expect(replay(List.filled(12, 5.0)), [1, 1, 1]);
    });

    test('a real reversal commits its own packet in the new direction', () {
      // The pending 15 px commits on the flip (rounded, as a touch run is), then the
      // reversal is its own packet.
      expect(replay([10, 5, -20]), [1, -1]);
      expect(replay([20, 5, -20]), [1, -1]);
    });

    test('a single 5 px wheel emits nothing', () {
      expect(replay([5]), isEmpty);
    });
  });
}
