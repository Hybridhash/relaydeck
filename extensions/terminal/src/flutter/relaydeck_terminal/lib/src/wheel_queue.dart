import 'dart:async';

/// Bounded paced queue for alternate-buffer wheel reports.
///
/// A flick in an agent TUI crosses dozens of lines; sending one INPUT packet
/// per line would serialize dozens of round trips and redraws. This queue
/// holds at most [maxPending] rows and releases [rowsPerFlush] per tick, so a
/// fling degrades to a bounded trailing motion instead of a catch-up burst.
/// Pure `dart:async` (no Flutter imports).
class WheelQueue {
  WheelQueue({required void Function(String report) send}) : _send = send;

  /// Most rows ever held; older excess is dropped, never queued behind input.
  static const int maxPending = 12;

  /// Rows released per flush: one report per packet, three packets per tick.
  static const int rowsPerFlush = 3;

  /// Pacing between timed flushes while rows remain.
  static const Duration flushInterval = Duration(milliseconds: 33);

  final void Function(String report) _send;

  int _pendingRows = 0;
  bool? _towardOlder;
  String _report = '';
  Timer? _tick;

  /// Rows currently waiting for a timed flush.
  int get pendingRows => _pendingRows;

  /// Adds [rows] wheel lines for [towardOlder], reported with [report] exactly
  /// as xterm2 produced it. A direction change replaces the pending run: the
  /// finger reversed, so the old direction is stale.
  void enqueue(bool towardOlder, int rows, String report) {
    if (rows <= 0) {
      return;
    }
    if (_towardOlder != towardOlder) {
      _towardOlder = towardOlder;
      _report = report;
      _pendingRows = rows < maxPending ? rows : maxPending;
    } else {
      _report = report;
      _pendingRows += rows;
      if (_pendingRows > maxPending) {
        _pendingRows = maxPending;
      }
    }
    if (_tick == null) {
      // Idle: answer at once, then pace the rest so motion starts instantly.
      // The tick arms even at zero: a fling's reports arrive as separate
      // enqueues, and the armed tick is what keeps them pacing instead of
      // each seeing an idle queue. It cancels itself on the first flush.
      flush();
      _tick = Timer.periodic(flushInterval, (_) => flush());
    }
  }

  /// Releases up to [rowsPerFlush] pending rows through the sender, one report
  /// per call: Python accepts exactly one report per INPUT packet.
  void flush() {
    var n = _pendingRows;
    if (n > rowsPerFlush) {
      n = rowsPerFlush;
    }
    // Debited before sending: the sender may discard (fence) or enqueue
    // (fresh input) reentrantly, and the count must stay exact either way.
    _pendingRows -= n;
    for (var i = 0; i < n; i++) {
      _send(_report);
    }
    if (_pendingRows <= 0) {
      _pendingRows = 0;
      _tick?.cancel();
      _tick = null;
    }
  }

  /// Drops everything pending and stops the tick: used when input, a mode
  /// boundary or a reset makes the queued rows stale.
  void discard() {
    _pendingRows = 0;
    _tick?.cancel();
    _tick = null;
  }
}
