import 'package:xterm2/xterm.dart';

/// Emulator-truth text snapshot for the Select Text sheet.
///
/// Grid-truth lines: `BufferLine.getText` DROPS empty cells (it skips
/// codePoint 0), so cursor-addressed content would lose its leading
/// position ('         X' reads back as 'X'). Each line's leading grid
/// offset is restored from per-cell reads while keeping getText's
/// content (combining marks, wide glyphs). Wrapped lines join exactly
/// like `Buffer.getText` (no newline between a line and its
/// continuation); trailing whitespace is stripped per line and trailing
/// empty lines are dropped, so the snapshot is a clean copy source.
/// Reads the ACTIVE buffer (alt screen shows alt content only, no
/// scrollback) — mirroring the Python `document_from_projection` rule.
///
/// Budget: Python rejects any snapshot whose declared total exceeds
/// MAX_SNAPSHOT_TOTAL (4 MiB, see protocol.py) with a visible error
/// instead of allocating, so engines must keep maxLines x cols within
/// that budget; truths are never silently truncated.
List<String> snapshotLines(Terminal terminal) {
  final raws = <String>[];
  final current = StringBuffer();
  var started = false;
  for (var i = 0; i < terminal.buffer.lines.length; i++) {
    final line = terminal.buffer.lines[i];
    if (started && !line.isWrapped) {
      raws.add(current.toString());
      current.clear();
    }
    started = true;
    var pad = 0;
    while (pad < line.length && line.getCodePoint(pad) == 0) {
      pad++;
    }
    current.write(' ' * pad);
    current.write(line.getText());
  }
  if (started) raws.add(current.toString());
  final lines = raws.map((l) {
    var end = l.length;
    while (end > 0 && (l[end - 1] == ' ' || l[end - 1] == '\t')) {
      end--;
    }
    return l.substring(0, end);
  }).toList();
  while (lines.isNotEmpty && lines.last.isEmpty) {
    lines.removeLast();
  }
  return lines;
}

/// The frozen sheet document: snapshot lines joined with newlines.
String snapshotText(Terminal terminal) => snapshotLines(terminal).join('\n');
