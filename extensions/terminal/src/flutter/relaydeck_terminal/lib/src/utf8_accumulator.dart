import 'dart:convert';

/// Incremental UTF-8 decoder holding split multibyte sequences across
/// deliveries. Mirrors the Python preview decoder's contract: never emit
/// a partial sequence; malformed bytes become U+FFFD, never an exception.
class Utf8Accumulator {
  final List<int> _carry = [];

  String add(List<int> chunk) {
    final bytes = [..._carry, ...chunk];
    _carry.clear();
    // Hold back an incomplete trailing multibyte sequence.
    var cut = bytes.length;
    var i = bytes.length - 1;
    var cont = 0;
    while (i >= 0 && (bytes[i] & 0xC0) == 0x80) {
      cont++;
      i--;
    }
    if (i >= 0) {
      final lead = bytes[i];
      int need;
      if ((lead & 0x80) == 0) {
        need = 0;
      } else if ((lead & 0xE0) == 0xC0) {
        need = 1;
      } else if ((lead & 0xF0) == 0xE0) {
        need = 2;
      } else if ((lead & 0xF8) == 0xF0) {
        need = 3;
      } else {
        need = 0;
      }
      if (cont < need) cut = i;
    } else if (cont > 0) {
      cut = 0;
    }
    _carry.addAll(bytes.sublist(cut));
    // allowMalformed decodes lone/invalid bytes as U+FFFD instead of
    // throwing on untrusted output; complete sequences decode exactly.
    return utf8.decode(bytes.sublist(0, cut), allowMalformed: true);
  }

  void reset() => _carry.clear();
}
