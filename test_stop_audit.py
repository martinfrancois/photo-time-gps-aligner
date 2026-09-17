"""Stop markers are approximate references, not assumed stop-start times."""

from pathlib import Path
import tempfile
import unittest

from audit_stops import audit


class StopAuditTests(unittest.TestCase):
    def test_marker_at_end_and_inside_consecutive_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.gpx"
            path.write_text(
                """<gpx creator="Geo Tracker"><wpt lat="0" lon="0"><time>2030-01-01T00:02:00Z</time><name>Stop for ~2 min</name></wpt><wpt lat="0" lon="0"><time>2030-01-01T00:10:00Z</time><name>Stop for ~7 min</name></wpt><trk><trkseg><trkpt lat="0" lon="0"><time>2030-01-01T00:00:00Z</time></trkpt><trkpt lat="0" lon="0"><time>2030-01-01T00:02:00Z</time></trkpt><trkpt lat="0" lon="0.00005"><time>2030-01-01T00:02:01Z</time></trkpt><trkpt lat="0" lon="0"><time>2030-01-01T00:07:00Z</time></trkpt><trkpt lat="0" lon="0"><time>2030-01-01T00:10:00Z</time></trkpt><trkpt lat="0" lon="0"><time>2030-01-01T00:14:00Z</time></trkpt></trkseg></trk></gpx>"""
            )
            report = audit(path)
            self.assertEqual(report["stop_count"], 2)
            self.assertEqual(
                [g["seconds"] for g in report["stops"][0]["adjacent_gaps"]], [120]
            )
            self.assertEqual(
                [g["seconds"] for g in report["stops"][1]["adjacent_gaps"]], [180, 240]
            )
            self.assertEqual(report["stops"][1]["combined_adjacent_gap_seconds"], 420)
            self.assertTrue(
                all(
                    g["gpx_only_midpoint_matched"]
                    for s in report["stops"]
                    for g in s["adjacent_gaps"]
                )
            )
            self.assertEqual(len(report["long_gaps_without_stop_marker"]), 1)
