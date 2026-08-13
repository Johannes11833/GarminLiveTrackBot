import 'package:flutter_test/flutter_test.dart';
import 'package:garmin_livetrack_viewer/main.dart';

void main() {
  testWidgets('renders the LiveTrack viewer', (tester) async {
    await tester.pumpWidget(const LiveTrackApp());
    expect(find.text('Garmin LiveTrack'), findsOneWidget);
  });
}
