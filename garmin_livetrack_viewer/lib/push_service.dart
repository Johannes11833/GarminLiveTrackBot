import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:web/web.dart' as web;

enum PushStatus { unsupported, denied, enabled, failed }

class PushService extends ChangeNotifier {
  PushService({required this.apiBaseUrl, required this.token});

  final String apiBaseUrl;
  final String token;

  PushStatus _status = PushStatus.unsupported;
  String? _error;
  bool _busy = false;
  web.PushSubscription? _subscription;

  PushStatus get status => _status;
  String? get error => _error;
  bool get busy => _busy;

  Uri _apiUri(String path) {
    if (apiBaseUrl.isNotEmpty) return Uri.parse('$apiBaseUrl$path');
    return Uri.base.replace(path: path, query: null, fragment: null);
  }

  bool get _supported {
    if (!web.window.isSecureContext) return false;
    final window = web.window as JSObject;
    final navigator = web.window.navigator as JSObject;
    return window.hasProperty('Notification'.toJS).toDart &&
        window.hasProperty('PushManager'.toJS).toDart &&
        navigator.hasProperty('serviceWorker'.toJS).toDart;
  }

  Future<void> init() async {
    if (!_supported) {
      _status = PushStatus.unsupported;
      notifyListeners();
      return;
    }
    if (web.Notification.permission == 'granted') {
      try {
        final registration = await web.window.navigator.serviceWorker.ready.toDart;
        _subscription = await registration.pushManager.getSubscription().toDart;
        if (_subscription != null) {
          // Re-register on the backend: it may have forgotten the
          // subscription (e.g. after a server restart).
          await _sendSubscription();
          _status = PushStatus.enabled;
        } else {
          _status = PushStatus.unsupported;
        }
      } catch (_) {
        _status = PushStatus.unsupported;
      }
    } else if (web.Notification.permission == 'denied') {
      _status = PushStatus.denied;
    }
    notifyListeners();
  }

  Future<void> enable() async {
    if (_busy || !_supported || web.Notification.permission == 'denied') return;
    _busy = true;
    _error = null;
    notifyListeners();
    try {
      if (web.Notification.permission != 'granted') {
        final permission =
            (await web.Notification.requestPermission().toDart).toDart;
        if (permission != 'granted') {
          _status = PushStatus.denied;
          return;
        }
      }
      final registration = await web.window.navigator.serviceWorker
          .register('push-sw.js'.toJS)
          .toDart;
      await _waitForActivation(registration);
      _subscription = await registration.pushManager.getSubscription().toDart;
      if (_subscription == null) {
        final publicKey = await _fetchPublicKey();
        _subscription = await registration.pushManager
            .subscribe(
              web.PushSubscriptionOptionsInit(
                userVisibleOnly: true,
                applicationServerKey: publicKey.toJS,
              ),
            )
            .toDart;
      }
      await _sendSubscription();
      _status = PushStatus.enabled;
    } catch (error) {
      _status = PushStatus.failed;
      _error = error.toString();
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  Future<void> _waitForActivation(web.ServiceWorkerRegistration registration) async {
    for (var i = 0; i < 40; i++) {
      final active = registration.active;
      if (active != null && active.state == 'activated') return;
      await Future<void>.delayed(const Duration(milliseconds: 250));
    }
    throw Exception('Service worker did not activate.');
  }

  Future<Uint8List> _fetchPublicKey() async {
    final response = await http.get(_apiUri('/push/public-key'));
    if (response.statusCode != 200) {
      throw Exception('Failed to fetch VAPID public key (HTTP ${response.statusCode}).');
    }
    final key = jsonDecode(response.body)['publicKey'] as String;
    return base64Url.decode(base64Url.normalize(key));
  }

  Future<void> _sendSubscription() async {
    final subscription = _subscription!;
    final p256dh = subscription.getKey('p256dh')?.toDart.asUint8List();
    final auth = subscription.getKey('auth')?.toDart.asUint8List();
    if (p256dh == null || auth == null) {
      throw Exception('Subscription is missing encryption keys.');
    }
    final body = jsonEncode({
      'token': token,
      'subscription': {
        'endpoint': subscription.endpoint,
        'keys': {
          'p256dh': base64Url.encode(p256dh),
          'auth': base64Url.encode(auth),
        },
      },
    });
    final response = await http.post(
      _apiUri('/push/subscribe'),
      headers: {'Content-Type': 'application/json'},
      body: body,
    );
    if (response.statusCode == 403) {
      throw Exception('Invalid registration token.');
    }
    if (response.statusCode != 201) {
      throw Exception('Subscription rejected (HTTP ${response.statusCode}).');
    }
  }
}
