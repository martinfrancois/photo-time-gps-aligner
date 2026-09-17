"""Synthetic tests for standalone GPX stop estimates and optional Timeline."""

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

import photo_time_gps as a
from matching import match
from timeline import Timeline
from test_timeline import signal


class GapTests(unittest.TestCase):
    start = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)

    def tracks(
        self, seconds=300, separation_m=10, creator="Geo Tracker", policy="auto"
    ):
        t = self.start.timestamp()
        return a.TrackSegments(
            [[(t, 0, 0, None), (t + seconds, 0, separation_m / 111194.9266, None)]],
            creator,
            policy,
        )

    def match(self, tracks, seconds, timeline=None, radius=50):
        return match(
            tracks,
            timeline,
            self.start + dt.timedelta(seconds=seconds),
            60,
            600,
            radius,
            3600,
            a.position,
            a.unmatched_reason,
        )

    def test_five_observed_stop_gap_patterns_without_timeline(self):
        # Durations/displacements observed around five annotated stops; synthetic coordinates.
        for seconds, separation in [
            (133, 6.2),
            (194, 38.5),
            (76, 6.7),
            (443, 50.3),
            (143, 2.8),
            (315, 1.9),
            (104, 10.2),
            (483, 3.4),
        ]:
            with self.subTest(seconds=seconds):
                result, why = self.match(self.tracks(seconds, separation), seconds / 2)
                self.assertIsNone(why)
                self.assertEqual(result["method"], "stationary_estimate")
                self.assertTrue(result["approximate"])
                self.assertIsNone(result["altitude"])

    def test_radius_is_measured_from_center_and_position_is_constant(self):
        tracks = self.tracks(300, 80)
        early, _ = self.match(tracks, 1)
        late, _ = self.match(tracks, 299)
        self.assertEqual((early["lat"], early["lon"]), (late["lat"], late["lon"]))
        self.assertAlmostEqual(early["observed_radius_meters"], 40, places=4)
        self.assertIsNone(self.match(self.tracks(300, 101), 150)[0])

    def test_time_segment_and_recorder_guards(self):
        for tracks, seconds, radius in [
            (self.tracks(3601), 100, 50),
            (self.tracks(), -1, 50),
            (self.tracks(), 301, 50),
            (self.tracks(), 100, 0),
            (self.tracks(creator="Unknown"), 100, 50),
            (self.tracks(policy="strict"), 100, 50),
        ]:
            self.assertIsNone(self.match(tracks, seconds, radius=radius)[0])
        split = self.tracks()
        split[:] = [[split[0][0]], [split[0][1]]]
        self.assertIsNone(self.match(split, 100)[0])
        self.assertIsNotNone(
            self.match(self.tracks(creator="Unknown", policy="stationary"), 100)[0]
        )

    def test_observed_excursion_is_not_bridged(self):
        tracks = self.tracks(600)
        tracks[0].insert(1, (self.start.timestamp() + 300, 0, 0.01, None))
        self.assertIsNone(self.match(tracks, 150)[0])
        self.assertIsNone(self.match(tracks, 450)[0])

    def test_timeline_is_independent_alternative(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text(
                json.dumps(
                    {
                        "rawSignals": [
                            signal("2030-01-01T00:00:00Z"),
                            signal("2030-01-01T00:05:00Z", point="10°, 20.001°"),
                        ]
                    }
                )
            )
            result, _ = self.match([], 120, Timeline(path))
            self.assertEqual(result["source"], "Google Timeline rawSignals.position")
            self.assertTrue(result["approximate"])
        result, _ = self.match(self.tracks(30), 10)
        self.assertEqual(result["source"], "GPX")
        self.assertEqual(result["method"], "interpolated")

    def test_both_sources_priority_and_optional_contradictions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text(
                json.dumps(
                    {
                        "rawSignals": [
                            signal("2030-01-01T00:00:00Z"),
                            signal("2030-01-01T00:05:00Z"),
                        ]
                    }
                )
            )
            timeline = Timeline(path)
            # Available GPX wins even if Timeline disagrees.
            result, _ = self.match(self.tracks(30), 10, timeline)
            self.assertEqual(result["source"], "GPX")
            # A Timeline match takes priority over an inferred GPX stop.
            result, _ = self.match(self.tracks(), 120, timeline)
            self.assertEqual(result["source"], "Google Timeline rawSignals.position")
            # Sparse contradictory observations veto a GPX stop, without inventing a position.
            path.write_text(
                json.dumps(
                    {
                        "rawSignals": [
                            signal("2030-01-01T00:01:00Z"),
                            signal("2030-01-01T00:39:00Z"),
                        ]
                    }
                )
            )
            result, reason = self.match(self.tracks(2400), 1200, Timeline(path))
            self.assertIsNone(result)
            self.assertIn("contradict", reason)
            # No observations during the gap do not create a dependency on Timeline.
            path.write_text(
                json.dumps({"rawSignals": [signal("2030-01-02T00:00:00Z")]})
            )
            result, _ = self.match(self.tracks(2400), 1200, Timeline(path))
            self.assertEqual(result["method"], "stationary_estimate")
