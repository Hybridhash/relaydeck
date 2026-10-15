import 'dart:typed_data';

/// ACK (0xFF) encoding for the RelayDeck terminal extension.
///
/// Pure `dart:typed_data` (no Flutter imports) so this unit stays
/// compilable to JavaScript in isolation. dart2js throws for
/// `ByteData.setUint64`/`getUint64`, so encode the u64be body as two
/// supported u32be halves to keep ACK delivery progressing.
const kAckKind = 0xFF;

/// Largest exactly-representable ACK value on dart2js, where `int` is
/// a double (2^53 - 1, ~9 PB of terminal output, unreachable in
/// practice). Counter values beyond this saturate AT this bound (see
/// [encodeAck]) — an under-ACK, never an over-ACK.
const kMaxAckBytes = 9007199254740991;

/// Encode an ACK packet for [applied] cumulative applied output bytes.
///
/// - Negative input clamps to 0 (a counter can never go backwards).
/// - Values above [kMaxAckBytes] saturate AT [kMaxAckBytes]
///   (under-ACK): Python's backpressure accounting stays conservative
///   (unacked keeps growing past the bound) and no byte beyond the
///   actual cumulative count is ever acknowledged. Progress past the
///   bound needs a terminal reset, which zeroes both sides; the bound
///   is documented here rather than solved, since ~9 PB of output
///   cannot occur on a live viewer.
/// - All other values encode exactly (verified for small counters,
///   the 2^32 boundary, and 2^53 - 1 by the extension test suite, on
///   the VM and compiled to JavaScript).
Uint8List encodeAck(int applied) {
  var v = applied;
  if (v < 0) v = 0;
  if (v > kMaxAckBytes) v = kMaxAckBytes;
  final int hi = v ~/ 0x100000000;
  final int lo = v % 0x100000000;
  final data = ByteData(9)
    ..setUint8(0, kAckKind)
    ..setUint32(1, hi, Endian.big)
    ..setUint32(5, lo, Endian.big);
  return data.buffer.asUint8List();
}
