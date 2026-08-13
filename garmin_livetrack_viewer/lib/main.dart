import 'dart:async';
import 'dart:convert';
import 'dart:developer';

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_map_vector_tiles/flutter_map_vector_tiles.dart' as vt;
import 'package:http/http.dart' as http;
import 'package:latlong2/latlong.dart';

const apiBaseUrl = 'http://127.0.0.1:8000';

// Configurable colors: position (live dot), track (route), course (planned).
const positionColor = Colors.deepPurple;
const trackColor = Colors.teal;
const courseColor = Colors.pink;

// Vector basemap: OpenFreeMap (free, no key).
const vectorStyleUrl = 'https://tiles.openfreemap.org/styles/liberty';

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
  static const _centerZoom = 12.0;

  final _mapController = MapController();
  late final AnimationController _cameraAnimation = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 600),
  );
  Timer? _timer;
  List<Map<String, dynamic>> _trackings = [];
  List<LatLng> _track = [];
  List<LatLng> _course = [];
  String? _selectedId;
  String _status = 'Loading trackings...';
  bool _mapReady = false;
  bool _userInteracted = false;
  bool _hasPositionedMap = false;
  bool _refreshing = false;
  String _lastFingerprint = '';
  vt.Style? _vectorStyle;

  @override
  void initState() {
    super.initState();
    _refresh();
    _loadVectorStyle();
    _timer = Timer.periodic(const Duration(seconds: 5), (_) => _refresh());
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
    _vectorStyle?.dispose();
    _cameraAnimation.dispose();
    _timer?.cancel();
    super.dispose();
  }

  Future<List<dynamic>> _getList(String path) async {
    final response = await http.get(Uri.parse('$apiBaseUrl$path'));
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

  Future<void> _refresh() async {
    if (_refreshing) return;
    _refreshing = true;
    try {
      final trackings = (await _getList('/trackings'))
          .whereType<Map<String, dynamic>>()
          .toList();
      if (trackings.isEmpty) {
        if (mounted && _status != 'No active tracking sessions.') {
          setState(() {
            _trackings = [];
            _selectedId = null;
            _track = [];
            _course = [];
            _status = 'No active tracking sessions.';
          });
        }
        return;
      }
      final ids = trackings.map((tracking) => tracking['id'] as String).toSet();
      final selectedId = ids.contains(_selectedId) ? _selectedId! : ids.first;
      final data = await Future.wait([
        _getList('/trackings/$selectedId/track'),
        _getList('/trackings/$selectedId/course'),
      ]);
      if (!mounted) return;
      final tracking = trackings.firstWhere((item) => item['id'] == selectedId);
      final track = _coordinates(data[0]);
      final course = _coordinates(data[1]);
      final status = '${tracking['state']} | ${track.length} track points';
      final fingerprint =
          '$selectedId|${track.length}|${course.length}|'
          '${track.isEmpty ? '' : track.last}|'
          '${course.isEmpty ? '' : course.first}|$status';
      if (fingerprint == _lastFingerprint) {
        // Retry a pending auto-center even when nothing changed.
        _centerMapOnce(track, course);
        return;
      }
      _lastFingerprint = fingerprint;
      setState(() {
        _trackings = trackings;
        // Don't revert a session the user selected mid-poll.
        if (_selectedId == null || _selectedId == selectedId) {
          _selectedId = selectedId;
        }
        _track = track;
        _course = course;
        _status = status;
      });
      _centerMapOnce(track, course);
    } catch (error, stackTrace) {
      log("$error");
      log("$stackTrace");
      if (mounted) setState(() => _status = 'Cannot reach API: $error');
    } finally {
      _refreshing = false;
    }
  }

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

  void _selectTracking(String? id) {
    if (id == null || id == _selectedId) return;
    setState(() => _selectedId = id);
    _refresh().then((_) {
      // Center on the newly selected session unless the user already panned.
      if (_refreshing || !mounted || !_mapReady || _userInteracted) return;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        final target = _track.isNotEmpty
            ? _track.last
            : (_course.isNotEmpty ? _course.first : null);
        if (target == null) return;
        try {
          _mapController.move(target, _centerZoom);
        } catch (_) {}
      });
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
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('Garmin LiveTrack'),
      actions: [
        IconButton(onPressed: _refresh, icon: const Icon(Icons.refresh)),
      ],
    ),
    body: Column(
      children: [
        Padding(
          padding: const EdgeInsets.all(12),
          child: Row(
            children: [
              Expanded(
                child: DropdownButtonFormField<String>(
                  key: ValueKey(_selectedId),
                  initialValue: _selectedId,
                  hint: const Text('Select a tracking session'),
                  items: _trackings
                      .map(
                        (tracking) => DropdownMenuItem(
                          value: tracking['id'] as String,
                          child: Text(
                            '${tracking['id']} (${tracking['state']})',
                          ),
                        ),
                      )
                      .toList(),
                  onChanged: _selectTracking,
                ),
              ),
              const SizedBox(width: 16),
              Text(_status),
            ],
          ),
        ),
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
                right: 12,
                bottom: 12,
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    FloatingActionButton.small(
                      heroTag: 'focusPosition',
                      tooltip: 'Focus current position',
                      onPressed: _focusPosition,
                      child: const Icon(Icons.my_location),
                    ),
                    const SizedBox(height: 8),
                    FloatingActionButton.small(
                      heroTag: 'zoomIn',
                      tooltip: 'Zoom in',
                      onPressed: () => _zoomBy(1),
                      child: const Icon(Icons.add),
                    ),
                    const SizedBox(height: 8),
                    FloatingActionButton.small(
                      heroTag: 'zoomOut',
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
