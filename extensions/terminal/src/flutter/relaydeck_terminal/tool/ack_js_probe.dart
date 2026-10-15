// Web-compiled encoder check: VM tests cannot catch dart2js-only
// runtime throws such as ByteData.setUint64. Run via tool/check_ack_js.sh:
// compiles this probe with
// `dart compile js` and executes it under node, diffing exact bytes.
import '../lib/src/ack.dart';

String _hex(List<int> bytes) =>
    bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();

void main() {
  for (final v in [
    0,
    52,
    0xFFFFFFFF,
    0x100000000,
    0x100000001,
    9007199254740991, // 2^53 - 1: exact
    9007199254740992, // 2^53: saturates
    -1, // clamps to 0
  ]) {
    // ignore: avoid_print
    print('$v:${_hex(encodeAck(v))}');
  }
}
