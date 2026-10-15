import 'dart:async';

import 'package:app_links/app_links.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flet/flet.dart';
import 'package:flutter/foundation.dart';

/// Background tap handler entry point. Must stay a top-level function and
/// keep the vm:entry-point pragma so Android can wake it.
@pragma('vm:entry-point')
Future<void> relayDeckMessagingBackgroundHandler(RemoteMessage message) async {
  await Firebase.initializeApp();
  // Best-effort only: the message is re-read through getInitialMessage /
  // onMessageOpenedApp when the user taps. Nothing is executed here and no
  // transcript is stored.
  debugPrint('relaydeck: background message ${message.messageId}');
}

class RelayDeckNotificationsService extends FletService {
  RelayDeckNotificationsService({required super.control});

  StreamSubscription<RemoteMessage>? _openedAppSub;
  StreamSubscription<String>? _tokenSub;
  StreamSubscription<Uri>? _linksSub;
  final _appLinks = AppLinks();
  bool _messagingReady = false;
  bool _disposed = false;
  late final Future<void> _messagingInit;

  @override
  void init() {
    super.init();
    control.addInvokeMethodListener(_invokeMethod);
    _messagingInit = _initMessaging();
    _initLinks();
  }

  Future<void> _initMessaging() async {
    try {
      await Firebase.initializeApp();
      if (_disposed) return;
      FirebaseMessaging.onBackgroundMessage(
        relayDeckMessagingBackgroundHandler,
      );
      _tokenSub =
          FirebaseMessaging.instance.onTokenRefresh.listen((token) {
        if (_disposed) return;
        control.triggerEvent('push_token', {'token': token});
      });
      _openedAppSub =
          FirebaseMessaging.onMessageOpenedApp.listen(_onOpenedMessage);
      _messagingReady = true;
    } catch (e) {
      // No Firebase project configured (or Google services missing):
      // push stays unavailable and the app remains usable.
      debugPrint('relaydeck: messaging unavailable');
    }
  }

  Future<void> _initLinks() async {
    try {
      final initial = await _appLinks.getInitialLink();
      if (_disposed) return;
      if (initial != null) _onAppLink(initial);
    } catch (e) {
      debugPrint('relaydeck: initial link unavailable');
    }
    _linksSub = _appLinks.uriLinkStream.listen(
      _onAppLink,
      onError: (Object e) => debugPrint('relaydeck: link stream unavailable'),
    );
  }

  void _onOpenedMessage(RemoteMessage message) {
    if (_disposed) return;
    control.triggerEvent('push_tap', {
      'notification_id': message.data['notification_id'] ?? '',
      'event_ref': message.data['event_ref'] ?? '',
      'category': message.data['category'] ?? '',
    });
  }

  void _onAppLink(Uri uri) {
    if (_disposed) return;
    // Only relaydeck app-link hosts are accepted; the Python side
    // validates the path (OAuth callback vs deep link) before acting.
    control.triggerEvent('app_link', {'uri': uri.toString()});
  }

  Future<dynamic> _invokeMethod(String name, dynamic args) async {
    await _messagingInit;
    if (_disposed) return null;
    switch (name) {
      case 'permission':
        if (!_messagingReady) return 'unknown';
        final current = await FirebaseMessaging.instance.getNotificationSettings();
        switch (current.authorizationStatus) {
          case AuthorizationStatus.authorized:
          case AuthorizationStatus.provisional:
            return 'granted';
          case AuthorizationStatus.denied:
            return 'denied';
          case AuthorizationStatus.notDetermined:
            return 'unknown';
          default:
            // Includes deniedPermanently (firebase_messaging_platform_interface
            // 4.10+) and any future refusal state.
            return 'denied';
        }
      case 'request_permission':
        if (!_messagingReady) return false;
        final settings =
            await FirebaseMessaging.instance.requestPermission();
        return settings.authorizationStatus ==
            AuthorizationStatus.authorized;
      case 'get_token':
        if (!_messagingReady) return null;
        try {
          return await FirebaseMessaging.instance.getToken();
        } catch (e) {
          debugPrint('relaydeck: token unavailable');
          return null;
        }
      case 'delete_token':
        if (!_messagingReady) return null;
        await FirebaseMessaging.instance.deleteToken();
        return null;
      case 'initial_tap':
        if (!_messagingReady) return null;
        final message =
            await FirebaseMessaging.instance.getInitialMessage();
        if (message == null) return null;
        return {
          'notification_id': message.data['notification_id'] ?? '',
          'event_ref': message.data['event_ref'] ?? '',
          'category': message.data['category'] ?? '',
        };
      default:
        throw ArgumentError('unknown method $name');
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _openedAppSub?.cancel();
    _tokenSub?.cancel();
    _linksSub?.cancel();
    super.dispose();
  }
}
