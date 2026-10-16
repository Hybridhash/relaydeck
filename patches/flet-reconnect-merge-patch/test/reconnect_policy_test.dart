import 'package:flet/src/utils/reconnect_policy.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  int ms(Duration d) => d.inMilliseconds;

  test('delay sequence with fixed random is exact, then caps', () {
    final policy = ReconnectPolicy(random: () => 0.5);
    expect(ms(policy.nextDelay()), 300);
    expect(ms(policy.nextDelay()), 600);
    expect(ms(policy.nextDelay()), 1200);
    expect(ms(policy.nextDelay()), 2000);
    expect(ms(policy.nextDelay()), 2000);
  });

  test('jitter stays within +-25 % of the step', () {
    final low = ReconnectPolicy(random: () => 0.0);
    expect(ms(low.nextDelay()), 225);
    expect(ms(low.nextDelay()), 450);
    final high = ReconnectPolicy(random: () => 1.0);
    expect(ms(high.nextDelay()), 375);
    expect(ms(high.nextDelay()), 750);
    // A long run with real randomness never leaves the first-step band.
    final policy = ReconnectPolicy();
    for (var i = 0; i < 200; i++) {
      policy.reset();
      expect(ms(policy.nextDelay()), inInclusiveRange(225, 375));
    }
  });

  test('reset returns to the first step', () {
    final policy = ReconnectPolicy(random: () => 0.5);
    policy.nextDelay();
    policy.nextDelay();
    policy.reset();
    expect(ms(policy.nextDelay()), 300);
  });

  test('absence threshold is 15 s inclusive', () {
    final policy = ReconnectPolicy(random: () => 0.5);
    expect(
      policy.shouldReconnectAfter(const Duration(milliseconds: 14999)),
      isFalse,
    );
    expect(policy.shouldReconnectAfter(const Duration(seconds: 15)), isTrue);
    expect(policy.shouldReconnectAfter(Duration.zero), isFalse);
  });
}
