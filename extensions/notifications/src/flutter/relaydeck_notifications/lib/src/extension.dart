import 'package:flet/flet.dart';

import 'notifications.dart';

class Extension extends FletExtension {
  @override
  FletService? createService(Control control) {
    switch (control.type) {
      case "RelayDeckNotifications":
        return RelayDeckNotificationsService(control: control);
      default:
        return null;
    }
  }
}
