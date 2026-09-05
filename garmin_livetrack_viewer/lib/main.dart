import 'dart:async';
import 'dart:convert';
import 'dart:developer';

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_map_vector_tiles/flutter_map_vector_tiles.dart' as vt;
import 'package:http/http.dart' as http;
import 'package:latlong2/latlong.dart';
import 'package:web/web.dart' as web;

import 'push_service.dart';

// API origin; empty means "same origin" (production behind a reverse proxy).
// Override for local development, e.g.:
//   flutter run -d chrome --dart-define=API_BASE_URL=http://127.0.0.1:8000
const apiBaseUrl = String.fromEnvironment('API_BASE_URL');

const _senderNameStorageKey = 'livetrack_sender_name';

String? _loadSavedSenderName() {
  try {
    return web.window.localStorage.getItem(_senderNameStorageKey);
  } catch (_) {
    return null;
  }
}

void _saveSenderName(String name) {
  try {
    web.window.localStorage.setItem(_senderNameStorageKey, name);
  } catch (_) {}
}

// Configurable colors: position (live dot), track (route), course (planned).
const positionColor = Colors.deepPurple;
const buttonColor = Colors.deepPurple;
const trackColor = Colors.teal;
const courseColor = Colors.pink;

// Bright icons on dark button colors, dark icons on light button colors.
final buttonIconColor = buttonColor.computeLuminance() < 0.5
    ? Colors.white
    : Colors.black87;

// Vector basemap: OpenFreeMap (free, no key).
const vectorStyleUrl = 'https://tiles.openfreemap.org/styles/liberty';

String _twoDigits(int n) => n.toString().padLeft(2, '0');

String _formatTime(DateTime t) =>
    '${_twoDigits(t.hour)}:${_twoDigits(t.minute)}:${_twoDigits(t.second)}';

String _formatDuration(Object? value) {
  if (value is! num) return '';
  final total = value.toInt();
  return '${total ~/ 3600}:${_twoDigits((total % 3600) ~/ 60)}:'
      '${_twoDigits(total % 60)}';
}

String _formatDistance(Object? value) {
  if (value is! num) return '';
  if (value >= 1000) return '${(value / 1000).toStringAsFixed(2)} km';
  return '${value.toStringAsFixed(0)} m';
}

String _formatSpeed(Object? value) {
  if (value is! num) return '';
  return '${(value * 3.6).toStringAsFixed(1)} km/h';
}

String _formatElevation(Object? value) {
  if (value is! num) return '';
  return '${value.toStringAsFixed(0)} m';
}

String _formatHeartRate(Object? value) {
  if (value is! num) return '';
  return '${value.toStringAsFixed(0)} bpm';
}

DateTime? _parseIsoDateTime(Object? value) {
  if (value is! String || value.isEmpty) return null;
  return DateTime.tryParse(value)?.toLocal();
}

void main() => runApp(const LiveTrackApp());

class LiveTrackApp extends StatelessWidget {
  const LiveTrackApp({super.key});

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'Garmin LiveTrack Viewer',
    theme: ThemeData(colorSchemeSeed: Colors.teal, useMaterial3: true),
    home: const LiveTrackPage(),
  );
}

class LiveTrackPage extends StatefulWidget {
  const LiveTrackPage({super.key});

  @override
  State<LiveTrackPage> createState() => _LiveTrackPageState();
}

class _LiveTrackPageState extends State<LiveTrackPage>
    with SingleTickerProviderStateMixin {
  static const _centerZoom = 13.0;

  final _mapController = MapController();
  late final AnimationController _cameraAnimation = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 600),
  );
  Timer? _timer;
  List<LatLng> _track = [];
  List<LatLng> _course = [];
  bool _mapReady = false;
  bool _userInteracted = false;
  bool _hasPositionedMap = false;
  bool _refreshing = false;
  String _lastFingerprint = '';
  String _lastToast = '';
  Map<String, dynamic>? _session;
  Map<String, dynamic>? _metaData;
  DateTime? _lastUpdate;
  String? _trackerState;
  vt.Style? _vectorStyle;
  PushService? _pushService;

  Uri _apiUri(String path) {
    if (apiBaseUrl.isNotEmpty) return Uri.parse('$apiBaseUrl$path');
    return Uri.base.replace(path: path, query: null, fragment: null);
  }

  @override
  void initState() {
    super.initState();
    // Defer until after the first frame so ScaffoldMessenger is available
    // for toasts (e.g. when no ?id= is provided).
    WidgetsBinding.instance.addPostFrameCallback((_) => _refresh());
    _loadVectorStyle();
    _timer = Timer.periodic(const Duration(seconds: 5), (_) => _refresh());
    _initPush();
  }

  void _initPush() {
    final token = Uri.base.queryParameters['token'];
    if (token == null || token.trim().isEmpty) return;
    final service = PushService(apiBaseUrl: apiBaseUrl, token: token.trim());
    _pushService = service..addListener(_onPushChanged);
    service.init();
  }

  void _onPushChanged() {
    final service = _pushService;
    if (service == null || service.status != PushStatus.failed) return;
    _showToast('Notifications unavailable: ${service.error}');
  }

  Future<void> _enableNotifications() async {
    final service = _pushService;
    if (service == null) {
      _showToast('No registration token. Pass ?token=<token> to the app URL.');
      return;
    }
    await service.enable();
    switch (service.status) {
      case PushStatus.enabled:
        _showToast('Notifications enabled.');
      case PushStatus.denied:
        _showToast('Notifications blocked in browser settings.');
      case PushStatus.failed:
        _showToast('Notifications unavailable: ${service.error}');
      case PushStatus.unsupported:
        _showToast(
          'Notifications need HTTPS and a supported browser (iOS 16.4+ when installed).',
        );
    }
  }

  Future<void> _loadVectorStyle() async {
    try {
      final style = await vt.StyleReader(
        uri: vectorStyleUrl,
        logger: const vt.Logger.console(),
      ).read();
      if (!mounted) return;
      setState(() => _vectorStyle = style);
    } catch (error) {
      log('Failed to load vector style: $error');
    }
  }

  @override
  void dispose() {
    _pushService?.dispose();
    _vectorStyle?.dispose();
    _cameraAnimation.dispose();
    _timer?.cancel();
    super.dispose();
  }

  Future<List<dynamic>> _getList(String path) async {
    final response = await http.get(_apiUri(path));
    if (response.statusCode != 200) {
      throw Exception('API returned HTTP ${response.statusCode}.');
    }
    return jsonDecode(response.body) as List<dynamic>;
  }

  List<LatLng> _coordinates(List<dynamic> points) => points
      .whereType<Map<String, dynamic>>()
      .map(
        (point) => LatLng(
          (point['latitude'] as num).toDouble(),
          (point['longitude'] as num).toDouble(),
        ),
      )
      .toList();

  String? _resolveSessionId() {
    final idParam = Uri.base.queryParameters['id'];
    if (idParam != null && idParam.trim().isNotEmpty) return idParam.trim();
    return null;
  }

  String? _profileImageUrl(String sessionId) =>
      _apiUri('/trackings/$sessionId/profile-image').toString();

  Future<Map<String, dynamic>?> _getMap(String path) async {
    final response = await http.get(_apiUri(path));
    if (response.statusCode != 200) return null;
    final body = jsonDecode(response.body);
    return body is Map<String, dynamic> ? body : null;
  }

  String? _lastSenderName = _loadSavedSenderName();

  Future<bool> _sendMessage(String sender, String content) async {
    final sessionId = _resolveSessionId();
    if (sessionId == null) return false;
    _lastSenderName = sender;
    _saveSenderName(sender);
    try {
      final response = await http.post(
        _apiUri('/trackings/$sessionId/message'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'sender': sender, 'content': content}),
      );
      if (response.statusCode != 204) {
        throw Exception('API returned HTTP ${response.statusCode}.');
      }
      _showSnack('Message sent.');
      return true;
    } catch (error) {
      _showSnack('Failed to send message: $error');
      return false;
    }
  }

  void _showSnack(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(message)));
  }

  void _showToast(String message) {
    if (message == _lastToast) return;
    _lastToast = message;
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(
        SnackBar(content: Text(message), duration: const Duration(days: 1)),
      );
  }

  Future<void> _refresh() async {
    if (_refreshing) return;
    _refreshing = true;
    try {
      final sessionId = _resolveSessionId();
      if (sessionId == null) {
        _showToast('No session. Pass ?id=<session id> to the app URL.');
        return;
      }
      final snapshot = await _getMap('/trackings/$sessionId');
      if (snapshot == null) {
        _showToast('Tracking $sessionId not found.');
        return;
      }
      final data = await Future.wait([
        _getList('/trackings/$sessionId/track'),
        _getList('/trackings/$sessionId/course'),
      ]);
      if (!mounted) return;
      final trackData = data[0].whereType<Map<String, dynamic>>().toList();
      final track = _coordinates(trackData);
      final course = _coordinates(data[1]);
      final lastMeta = trackData.isNotEmpty ? trackData.last['metaData'] : null;
      final trackerState = snapshot['state']?.toString();
      final fingerprint =
          '$sessionId|${track.length}|${course.length}|'
          '${track.isEmpty ? '' : track.last}|'
          '${course.isEmpty ? '' : course.first}|$lastMeta|$trackerState';
      if (fingerprint == _lastFingerprint) {
        // Retry a pending auto-center even when nothing changed.
        _centerMapOnce(track, course);
        return;
      }
      _lastFingerprint = fingerprint;
      _lastToast = '';
      final lastTs = trackData.isNotEmpty ? trackData.last['timestamp'] : null;
      final lastUpdate = lastTs is num
          ? DateTime.fromMillisecondsSinceEpoch(
              // Garmin timestamps can be in seconds or milliseconds.
              lastTs >= 1000000000000 ? lastTs.toInt() : lastTs.toInt() * 1000,
            )
          : null;
      setState(() {
        _track = track;
        _course = course;
        _session = snapshot['session'] is Map<String, dynamic>
            ? snapshot['session'] as Map<String, dynamic>
            : null;
        _metaData = lastMeta is Map<String, dynamic> ? lastMeta : null;
        _lastUpdate = lastUpdate;
        _trackerState = trackerState;
      });
      _centerMapOnce(track, course);
    } catch (error, stackTrace) {
      log("$error");
      log("$stackTrace");
      _showToast('Cannot reach API: $error');
    } finally {
      _refreshing = false;
    }
  }

  List<(String, String)> _metaDataRows(Map<String, dynamic> meta) {
    final rows = <(String, String)>[];
    void add(String label, Object? value) {
      if (value == null) return;
      final text = value.toString().trim();
      if (text.isNotEmpty && text != 'null') rows.add((label, text));
    }

    add('Distance', _formatDistance(meta['TOTAL_DISTANCE']));
    add('Duration', _formatDuration(meta['TOTAL_DURATION']));
    add('Speed', _formatSpeed(meta['SPEED']));
    add('Elevation', _formatElevation(meta['ELEVATION']));
    add('Heart rate', _formatHeartRate(meta['HEART_RATE']));
    return rows;
  }

  Icon _metaIcon(String label) => switch (label) {
    'Distance' => const Icon(Icons.route, size: 18),
    'Duration' => const Icon(Icons.timer_outlined, size: 18),
    'Speed' => const Icon(Icons.speed, size: 18),
    'Elevation' => const Icon(Icons.terrain, size: 18),
    'Heart rate' => const Icon(Icons.favorite, size: 18),
    _ => const Icon(Icons.info_outline, size: 18),
  };

  void _centerMapOnce(List<LatLng> track, List<LatLng> course) {
    if (_hasPositionedMap || !_mapReady || _userInteracted) return;
    if (track.isEmpty && course.isEmpty) return;
    _hasPositionedMap = true;
    // Move after the frame so we never race an in-flight zoom gesture.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      try {
        _mapController.move(
          track.isNotEmpty ? track.last : course.first,
          _centerZoom,
        );
      } catch (_) {
        // Map may have been disposed; the next poll will retry.
      }
    });
  }

  void _animateCamera(
    LatLng target,
    double zoom, {
    Duration duration = const Duration(milliseconds: 600),
  }) {
    if (!_mapReady) return;
    final camera = _mapController.camera;
    if (camera.center == target && camera.zoom == zoom) return;
    final fromCenter = camera.center;
    final fromZoom = camera.zoom;
    LatLng lerp(LatLng a, LatLng b, double t) => LatLng(
      a.latitude + (b.latitude - a.latitude) * t,
      a.longitude + (b.longitude - a.longitude) * t,
    );
    void tick() {
      final t = Curves.easeInOut.transform(_cameraAnimation.value);
      try {
        _mapController.move(
          lerp(fromCenter, target, t),
          fromZoom + (zoom - fromZoom) * t,
        );
      } catch (_) {}
    }

    _cameraAnimation.stop();
    _cameraAnimation.duration = duration;
    _cameraAnimation.reset();
    _cameraAnimation.addListener(tick);
    _cameraAnimation.forward().whenComplete(() {
      if (mounted) _cameraAnimation.removeListener(tick);
    });
  }

  void _focusPosition() {
    if (!_mapReady) return;
    final target = _track.isNotEmpty
        ? _track.last
        : (_course.isNotEmpty ? _course.first : null);
    if (target == null) return;
    _animateCamera(target, _centerZoom);
  }

  void _zoomBy(double delta) {
    if (!_mapReady) return;
    _animateCamera(
      _mapController.camera.center,
      _mapController.camera.zoom + delta,
      duration: const Duration(milliseconds: 250),
    );
  }

  @override
  Widget build(BuildContext context) {
    final sessionName = _session?['sessionName']?.toString().trim();
    final sessionId = _resolveSessionId();
    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            const Text('Garmin LiveTrack'),
            if (sessionName != null && sessionName.isNotEmpty) ...[
              const Text(' - '),
              Flexible(
                child: Text(
                  sessionName,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.bodyMedium,
                ),
              ),
            ],
          ],
        ),
        actions: [
          _NotificationButton(
            service: _pushService,
            onPressed: _enableNotifications,
          ),
          IconButton(onPressed: _refresh, icon: const Icon(Icons.refresh)),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: Stack(
              children: [
                FlutterMap(
                  mapController: _mapController,
                  options: MapOptions(
                    initialCenter: const LatLng(0, 0),
                    initialZoom: 2,
                    onMapReady: () => _mapReady = true,
                    onMapEvent: (event) {
                      // Only real gestures count as user interaction; layout and
                      // programmatic events (nonRotatedSizeChange, mapController)
                      // fire on startup and must not block auto-centering.
                      if (event.source != MapEventSource.mapController &&
                          event.source != MapEventSource.nonRotatedSizeChange) {
                        _userInteracted = true;
                      }
                    },
                  ),
                  children: [
                    if (_vectorStyle != null)
                      vt.VectorTileLayer(
                        theme: _vectorStyle!.theme,
                        tileProviders: _vectorStyle!.providers,
                        rasterSources: _vectorStyle!.rasterSources,
                        sprites: _vectorStyle!.sprites,
                      ),
                    PolylineLayer(
                      polylines: [
                        if (_course.isNotEmpty)
                          Polyline(
                            points: _course,
                            color: courseColor,
                            strokeWidth: 4,
                          ),
                        if (_track.isNotEmpty)
                          Polyline(
                            points: _track,
                            color: trackColor,
                            strokeWidth: 4,
                          ),
                      ],
                    ),
                    if (_track.isNotEmpty)
                      MarkerLayer(
                        markers: [
                          Marker(
                            point: _track.first,
                            width: 28,
                            height: 28,
                            child: const _EndpointMarker(
                              icon: Icons.flag,
                              color: Colors.green,
                            ),
                          ),
                          if (_trackerState == 'ended')
                            Marker(
                              point: _track.last,
                              width: 28,
                              height: 28,
                              child: const _EndpointMarker(
                                icon: Icons.sports_score,
                                color: Colors.redAccent,
                              ),
                            )
                          else
                            Marker(
                              point: _track.last,
                              width: 72,
                              height: 72,
                              child: const _PulsingPositionMarker(),
                            ),
                        ],
                      ),
                  ],
                ),
                Positioned(
                  top: 12,
                  left: 12,
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      if (_metaData != null && _metaData!.isNotEmpty)
                        for (final entry in _metaDataRows(_metaData!)) ...[
                          Card(
                            margin: EdgeInsets.zero,
                            child: Padding(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 10,
                                vertical: 6,
                              ),
                              child: Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  _metaIcon(entry.$1),
                                  const SizedBox(width: 6),
                                  Text(
                                    entry.$2,
                                    style: Theme.of(context)
                                        .textTheme
                                        .bodyMedium
                                        ?.copyWith(fontWeight: FontWeight.w600),
                                  ),
                                ],
                              ),
                            ),
                          ),
                          const SizedBox(width: 6),
                        ],
                    ],
                  ),
                ),
                Positioned(
                  left: 12,
                  bottom: 12,
                  child: _LiveUserOverlay(
                    userName: _session?['userDisplayName']?.toString().trim(),
                    profileImageUrl: sessionId != null
                        ? _profileImageUrl(sessionId)
                        : null,
                    startTime: _parseIsoDateTime(_session?['start']),
                    lastUpdate: _lastUpdate,
                    ended: _trackerState == 'ended',
                    initialSender: _lastSenderName,
                    onSendMessage: _sendMessage,
                  ),
                ),
                Positioned(
                  right: 12,
                  bottom: 12,
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      FloatingActionButton.small(
                        heroTag: 'focusPosition',
                        backgroundColor: buttonColor,
                        foregroundColor: buttonIconColor,
                        tooltip: 'Focus current position',
                        onPressed: _focusPosition,
                        child: const Icon(Icons.my_location),
                      ),
                      const SizedBox(height: 8),
                      FloatingActionButton.small(
                        heroTag: 'zoomIn',
                        backgroundColor: buttonColor,
                        foregroundColor: buttonIconColor,
                        tooltip: 'Zoom in',
                        onPressed: () => _zoomBy(1),
                        child: const Icon(Icons.add),
                      ),
                      const SizedBox(height: 8),
                      FloatingActionButton.small(
                        heroTag: 'zoomOut',
                        backgroundColor: buttonColor,
                        foregroundColor: buttonIconColor,
                        tooltip: 'Zoom out',
                        onPressed: () => _zoomBy(-1),
                        child: const Icon(Icons.remove),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _NotificationButton extends StatelessWidget {
  const _NotificationButton({required this.service, required this.onPressed});

  final PushService? service;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    if (service == null) return const SizedBox.shrink();
    return ListenableBuilder(
      listenable: service!,
      builder: (context, _) {
        final status = service!.status;
        final (icon, tooltip) = switch (status) {
          PushStatus.enabled => (
            Icons.notifications_active,
            'Notifications enabled',
          ),
          PushStatus.denied => (
            Icons.notifications_off,
            'Notifications blocked',
          ),
          PushStatus.unsupported => (
            Icons.notifications_none,
            'Enable notifications',
          ),
          PushStatus.failed => (
            Icons.notifications_none,
            'Notifications unavailable',
          ),
        };
        final enabled = status != PushStatus.enabled && !service!.busy;
        return IconButton(
          icon: Icon(icon),
          tooltip: tooltip,
          onPressed: enabled ? onPressed : null,
        );
      },
    );
  }
}

class _LiveUserOverlay extends StatefulWidget {
  const _LiveUserOverlay({
    required this.userName,
    required this.onSendMessage,
    this.profileImageUrl,
    this.startTime,
    this.lastUpdate,
    this.ended = false,
    this.initialSender,
  });

  final String? userName;
  final String? profileImageUrl;
  final DateTime? startTime;
  final DateTime? lastUpdate;
  final bool ended;
  final String? initialSender;
  final Future<bool> Function(String sender, String content) onSendMessage;

  @override
  State<_LiveUserOverlay> createState() => _LiveUserOverlayState();
}

class _LiveUserOverlayState extends State<_LiveUserOverlay> {
  bool _composing = false;
  bool _sending = false;
  late final _senderController = TextEditingController(
    text: widget.initialSender,
  );
  final _contentController = TextEditingController();

  @override
  void dispose() {
    _senderController.dispose();
    _contentController.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final sender = _senderController.text.trim();
    final content = _contentController.text.trim();
    if (sender.isEmpty || content.isEmpty) return;
    setState(() => _sending = true);
    final success = await widget.onSendMessage(sender, content);
    if (!mounted) return;
    setState(() {
      _sending = false;
      if (success) {
        _composing = false;
        _contentController.clear();
      }
    });
  }

  void _cancel() {
    setState(() {
      _composing = false;
      _contentController.clear();
    });
  }

  @override
  Widget build(BuildContext context) {
    final userName = widget.userName;
    if (userName == null || userName.isEmpty) return const SizedBox.shrink();
    return ConstrainedBox(
      constraints: const BoxConstraints(maxWidth: 300),
      child: Card(
        margin: EdgeInsets.zero,
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  _ProfileAvatar(
                    imageUrl: widget.profileImageUrl,
                    ended: widget.ended,
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: RichText(
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      text: TextSpan(
                        style: Theme.of(context).textTheme.bodyMedium,
                        children: [
                          TextSpan(
                            text: userName,
                            style: const TextStyle(fontWeight: FontWeight.bold),
                          ),
                          TextSpan(
                            text: widget.ended
                                ? "'s LiveTrack session has ended"
                                : ' is live',
                          ),
                        ],
                      ),
                    ),
                  ),
                ],
              ),
              if (!widget.ended) ...[
                const SizedBox(height: 10),
                if (_composing) ...[
                  TextField(
                    controller: _senderController,
                    enabled: !_sending,
                    decoration: const InputDecoration(
                      labelText: 'Your name',
                      isDense: true,
                    ),
                    textInputAction: TextInputAction.next,
                  ),
                  const SizedBox(height: 8),
                  TextField(
                    controller: _contentController,
                    enabled: !_sending,
                    decoration: const InputDecoration(
                      labelText: 'Message',
                      isDense: true,
                    ),
                    minLines: 1,
                    maxLines: 3,
                    autofocus: true,
                    textInputAction: TextInputAction.send,
                    onSubmitted: (_) => _submit(),
                  ),
                  const SizedBox(height: 8),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
                      TextButton(
                        onPressed: _sending ? null : _cancel,
                        child: const Text('Cancel'),
                      ),
                      const SizedBox(width: 4),
                      FilledButton.icon(
                        onPressed: _sending ? null : _submit,
                        icon: _sending
                            ? const SizedBox(
                                width: 14,
                                height: 14,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                ),
                              )
                            : const Icon(Icons.send, size: 18),
                        label: const Text('Send'),
                      ),
                    ],
                  ),
                ] else
                  ElevatedButton.icon(
                    onPressed: () => setState(() => _composing = true),
                    icon: const Icon(Icons.send, size: 18),
                    label: const Text('Send message'),
                  ),
              ],
              if (widget.startTime != null || widget.lastUpdate != null) ...[
                const SizedBox(height: 8),
                Text(
                  [
                    if (widget.startTime != null)
                      'Started ${_formatTime(widget.startTime!)}',
                    if (widget.lastUpdate != null)
                      'Updated ${_formatTime(widget.lastUpdate!)}',
                  ].join('  •  '),
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

class _ProfileAvatar extends StatelessWidget {
  const _ProfileAvatar({required this.imageUrl, required this.ended});

  final String? imageUrl;
  final bool ended;

  static const _diameter = 40.0;

  @override
  Widget build(BuildContext context) {
    final backgroundColor = ended
        ? Theme.of(context).colorScheme.surfaceContainerHighest
        : Theme.of(context).colorScheme.primaryContainer;
    final fallbackIcon = Icon(
      Icons.directions_bike,
      color: ended ? Theme.of(context).disabledColor : null,
    );
    final url = imageUrl;
    if (url == null) {
      return CircleAvatar(
        backgroundColor: backgroundColor,
        child: fallbackIcon,
      );
    }
    return ClipOval(
      child: SizedBox(
        width: _diameter,
        height: _diameter,
        child: ColoredBox(
          color: backgroundColor,
          child: Image.network(
            url,
            fit: BoxFit.cover,
            errorBuilder: (context, error, stackTrace) =>
                Center(child: fallbackIcon),
            loadingBuilder: (context, child, progress) =>
                progress == null ? child : Center(child: fallbackIcon),
          ),
        ),
      ),
    );
  }
}

class _EndpointMarker extends StatelessWidget {
  const _EndpointMarker({required this.icon, required this.color});

  final IconData icon;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(
        color: color,
        shape: BoxShape.circle,
        border: Border.all(color: Colors.white, width: 2),
        boxShadow: const [
          BoxShadow(color: Colors.black26, blurRadius: 4, offset: Offset(0, 2)),
        ],
      ),
      child: Icon(icon, color: Colors.white, size: 16),
    );
  }
}

class _PulsingPositionMarker extends StatefulWidget {
  const _PulsingPositionMarker();

  @override
  State<_PulsingPositionMarker> createState() => _PulsingPositionMarkerState();
}

class _PulsingPositionMarkerState extends State<_PulsingPositionMarker>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(seconds: 2),
  )..repeat();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final dot = SizedBox(
      width: 24,
      height: 24,
      child: DecoratedBox(
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: positionColor,
          border: Border.all(color: Colors.white, width: 3),
          boxShadow: const [
            BoxShadow(
              color: Colors.black26,
              blurRadius: 4,
              offset: Offset(0, 2),
            ),
          ],
        ),
      ),
    );
    return AnimatedBuilder(
      animation: _controller,
      child: dot,
      builder: (context, child) {
        final t = _controller.value;
        return Stack(
          alignment: Alignment.center,
          children: [
            Container(
              width: 24 + 48 * t,
              height: 24 + 48 * t,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: positionColor.withValues(alpha: 0.30 * (1 - t)),
              ),
            ),
            child!,
          ],
        );
      },
    );
  }
}
