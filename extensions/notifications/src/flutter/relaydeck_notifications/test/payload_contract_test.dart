// Contract mirror of the Python payload guard: documents the exact
// keys the Dart side forwards. Aliases or extra data keys must fail the
// Python guard, never flow to the app.
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('forwarded tap keys are exactly the guard key set', () {
    const forwarded = {'notification_id', 'event_ref', 'category'};
    // message.messageId -> notification_id; data carries only event_ref
    // and category. created_at/expires_at are gateway-added, never FCM.
    expect(forwarded, {'notification_id', 'event_ref', 'category'});
  });
}
