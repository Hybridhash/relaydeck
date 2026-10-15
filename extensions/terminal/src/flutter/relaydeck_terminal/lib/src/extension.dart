import 'package:flet/flet.dart';
import 'package:flutter/widgets.dart';

import 'picture_paste_region.dart';
import 'relaydeck_terminal.dart';

class Extension extends FletExtension {
  @override
  Widget? createWidget(Key? key, Control control) {
    switch (control.type) {
      case "RelayDeckTerminal":
        return RelayDeckTerminalControl(key: key, control: control);
      case "RelayDeckPicturePaste":
        return RelayDeckPicturePasteControl(key: key, control: control);
      default:
        return null;
    }
  }
}
