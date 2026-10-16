import 'dart:math' as math;

/// Pure reconnect-delay policy: stepped delays with jitter, plus the absence
/// threshold that decides keep-or-reconnect on wake.
///
/// A page hidden briefly keeps its socket (the close event, if the OS killed
/// it, is delivered right after resume); only a long absence pays for a
/// proactive close and reconnect.
class ReconnectPolicy {
  ReconnectPolicy({
    this.steps = const [
      Duration(milliseconds: 300),
      Duration(milliseconds: 600),
      Duration(milliseconds: 1200),
      Duration(milliseconds: 2000),
    ],
    this.jitter = 0.25,
    this.absenceThreshold = const Duration(seconds: 15),
    double Function()? random,
  }) : assert(steps.isNotEmpty, 'ReconnectPolicy needs at least one step'),
       random = random ?? math.Random().nextDouble;

  /// Delay steps; the last step repeats forever (the cap).
  final List<Duration> steps;

  /// Jitter fraction: each delay is its step x (1 +/- jitter).
  final double jitter;

  /// Absence at or above which a wake reconnects instead of keeping the socket.
  final Duration absenceThreshold;

  /// Uniform [0, 1) source; injectable for tests.
  final double Function() random;

  int _step = 0;

  /// Next retry delay: the current step with jitter, then advance. Stays at
  /// the last step forever, so a long outage retries at a bounded rate.
  Duration nextDelay() {
    final step = steps[_step];
    if (_step < steps.length - 1) {
      _step++;
    }
    final factor = 1 + (random() - 0.5) * 2 * jitter;
    final ms = (step.inMilliseconds * factor).round();
    return Duration(milliseconds: ms);
  }

  /// Back to the first step: call on wake and on a successful register.
  void reset() {
    _step = 0;
  }

  /// Whether a page hidden for [absence] must reconnect on wake.
  bool shouldReconnectAfter(Duration absence) => absence >= absenceThreshold;
}
