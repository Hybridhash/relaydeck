# Android integration requirements (verify on first APK build)

`flet build` generates the Android shell from its Flutter template; these
entries must be present in the merged manifest (via template support or a
documented post-step — confirm which, do not hand-edit
`build/` output as the durable mechanism).

```xml
<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
<!-- RECEIVE is normal protection level, granted automatically -->
<uses-permission android:name="com.google.android.c2dm.permission.RECEIVE" />

<application ...>
  <!-- OAuth callback + notification deep links (verified app links).
       Host placeholders: owner approves the public gateway domain first. -->
  <intent-filter android:autoVerify="true">
    <action android:name="android.intent.action.VIEW" />
    <category android:name="android.intent.category.DEFAULT" />
    <category android:name="android.intent.category.BROWSABLE" />
    <data android:scheme="https" android:host="GATEWAY_DOMAIN" android:pathPrefix="/app/" />
  </intent-filter>
  <!-- Custom-scheme fallback for the OAuth callback (RFC 8252 private-use URI) -->
  <intent-filter>
    <action android:name="android.intent.action.VIEW" />
    <category android:name="android.intent.category.DEFAULT" />
    <category android:name="android.intent.category.BROWSABLE" />
    <data android:scheme="dev.relaydeck.app" android:host="oauth" />
  </intent-filter>
  <!-- FCM: firebase_messaging merges its MESSAGING_EVENT service, the
       background wakeup receiver, and notification defaults from the
       library manifest. No hand-written <service> is expected. -->
  <meta-data android:name="com.google.firebase.messaging.default_notification_channel_id"
             android:value="relaydeck_attention" />
</application>
```

`google-services.json` (owner-authorized test project only) belongs in the
Flutter app's `android/app/` at build time and must never be committed.
Background execution limits (Doze, force-stop, battery restriction) mean
delivery is best-effort.

## deep-link path contract (no new manifest entries)

Minted links are `https://GATEWAY_DOMAIN/app/n/{rdl_...}` (single-purpose
`open-event`, expiring, opaque). The `pathPrefix="/app/"` intent-filter
above already routes them to the app; the Dart `app_links` listener
forwards the URI, and the Python side resolves it only over the
authenticated `POST /v1/deep-links/resolve` route (logged-out, revoked,
forged, expired, and foreign-account links fail closed with no content).
Tapping fetches current authorized state and never sends terminal input.
Verified headless in `tests/unit/test_deep_links.py`; real-device proof
remains blocked (no Android toolchain or Firebase project here).
