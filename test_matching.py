"""Gap policies use synthetic tracks and do not assume that close endpoints prove a stop."""

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
    def test_stationary_corroboration_and_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            start = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)

            def instant(seconds):
                return start + dt.timedelta(seconds=seconds)

            def run(points, tracks=None, radius=50):
                path.write_text(
                    json.dumps(
                        {
                            "rawSignals": [
                                signal(instant(t).isoformat(), point=p)
                                for t, p in points
                            ]
                        }
                    )
                )
                timeline = Timeline(path)
                tracks = (
                    [
                        [
                            (instant(0).timestamp(), 10, 20, None),
                            (instant(2400).timestamp(), 10, 20, None),
                        ]
                    ]
                    if tracks is None
                    else tracks
                )
                return match(
                    tracks,
                    timeline,
                    instant(1850),
                    60,
                    600,
                    radius,
                    3600,
                    a.position,
                    a.unmatched_reason,
                )

            points = [(t, "10°, 20°") for t in [100, 500, 900, 1300, 1800]]
            result, _ = run(points)
            self.assertEqual(result["method"], "stationary_estimate")
            self.assertEqual(result["corroborating_samples"], 5)
            self.assertIsNone(run(points, radius=0)[0])
            self.assertIsNone(run(points[:2])[0])
            # Same endpoints with an intervening excursion must not imply stationary.
            self.assertIsNone(
                run([(t, "11°, 20°" if t == 900 else p) for t, p in points])[0]
            )
            # Never join separate GPX segments for stationary estimates.
            self.assertIsNone(
                run(
                    points,
                    [
                        [(instant(0).timestamp(), 10, 20, None)],
                        [(instant(2400).timestamp(), 10, 20, None)],
                    ],
                )[0]
            )

    def test_gpx_priority_and_generous_timeline(self):
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
            timeline = Timeline(path)
            instant = dt.datetime(2030, 1, 1, 0, 2, tzinfo=dt.timezone.utc)
            result, _ = match(
                [], timeline, instant, 60, 600, 50, 3600, a.position, a.unmatched_reason
            )
            self.assertTrue(result["approximate"])
            self.assertEqual(result["gap"], 300)
            self.assertIsNone(
                match(
                    [],
                    timeline,
                    instant,
                    60,
                    60,
                    50,
                    3600,
                    a.position,
                    a.unmatched_reason,
                )[0]
            )
            tracks = [
                [
                    (instant.timestamp() - 1, 30, 40, None),
                    (instant.timestamp() + 1, 30, 40, None),
                ]
            ]
            result, _ = match(
                tracks,
                timeline,
                instant,
                60,
                600,
                50,
                3600,
                a.position,
                a.unmatched_reason,
            )
            self.assertEqual(result["source"], "GPX")
            self.assertAlmostEqual(result["lat"], 30)
