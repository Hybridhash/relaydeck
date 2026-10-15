// Line accounting for wheel/trackpad swipes in a terminal that has no mouse
// reporting: pure so the thresholds can be tested without a widget.

class SwipeFlush {
  const SwipeFlush(this.lines, this.remainder);

  /// Whole lines to send (0: nothing crosses a line yet).
  final int lines;

  /// Signed pixels left in the run. Always zero or the same sign as the run, so a
  /// following same-direction delta is never taken for a direction flip.
  final double remainder;
}

/// Decides what an idle flush sends and what stays in the run.
///
/// A one-shot run (touch, direction flip) emits `round(|pixels| / lineHeight)`
/// lines and keeps nothing, as before. With [keepRemainder] (a wheel's idle flush)
/// only whole lines are emitted and the rest stays in the run: slow scrolling
/// accumulates, never advances faster than the pixels travelled, and the
/// remainder always keeps the run's sign, so a following same-direction delta is
/// never mistaken for a reversal.
SwipeFlush swipeFlushAccount(
  double pixels,
  double lineHeight, {
  required bool keepRemainder,
}) {
  if (pixels == 0 || lineHeight <= 0) return const SwipeFlush(0, 0);
  final distance = pixels.abs();
  if (!keepRemainder) {
    final lines = (distance / lineHeight).round();
    return SwipeFlush(lines < 1 ? 0 : lines, 0);
  }
  final lines = (distance / lineHeight).floor();
  if (lines < 1) return SwipeFlush(0, pixels);
  final rest = distance - lines * lineHeight;
  return SwipeFlush(lines, rest > 0 ? pixels.sign * rest : 0);
}
