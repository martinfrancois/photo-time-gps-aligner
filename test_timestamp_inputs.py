"""External timestamps retain microseconds and UTC offsets on supported Pythons."""

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from photo_time_gps import load_track
from timeline import Timeline


class TimestampInputTests(unittest.TestCase):
    def test_fractional_gpx_and_timeline_timestamps(self):
        fractions = {
            "1": 100000,
            "27": 270000,
            "001": 1000,
            "0012": 1200,
            "12345": 123450,
            "123456": 123456,
            "1234567": 123456,
        }
        offsets = {"Z": 0, "+05:30": 330, "-05:00": -300}
        with tempfile.TemporaryDirectory() as directory:
            gpx = Path(directory) / "track.gpx"
            timeline = Path(directory) / "timeline.json"
            for fraction, microsecond in fractions.items():
                for offset, minutes in offsets.items():
                    with self.subTest(fraction=fraction, offset=offset):
                        stamp = f"2030-07-09T19:01:32.{fraction}{offset}"
                        expected = dt.datetime(
                            2030,
                            7,
                            9,
                            19,
                            1,
                            32,
                            microsecond,
                            tzinfo=dt.timezone(dt.timedelta(minutes=minutes)),
                        ).timestamp()
                        gpx.write_text(
                            '<gpx><trk><trkseg><trkpt lat="10" lon="20">'
                            f"<time>{stamp}</time></trkpt></trkseg></trk></gpx>"
                        )
                        timeline.write_text(
                            json.dumps(
                                {
                                    "rawSignals": [
                                        {
                                            "position": {
                                                "timestamp": stamp,
                                                "LatLng": "10°, 20°",
                                                "accuracyMeters": 4,
                                            }
                                        }
                                    ]
                                }
                            )
                        )
                        self.assertEqual(load_track(gpx)[0][0][0], expected)
                        self.assertEqual(Timeline(timeline).times, [expected])

    def test_invalid_and_offset_free_timestamps_remain_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            gpx = Path(directory) / "track.gpx"
            for stamp in [
                "2030-02-30T19:01:32.27Z",
                "2030-07-09T19:01:32.badZ",
                "2030-07-09T19:01:32.27",
            ]:
                with self.subTest(stamp=stamp):
                    gpx.write_text(
                        '<gpx><trk><trkseg><trkpt lat="10" lon="20">'
                        f"<time>{stamp}</time></trkpt></trkseg></trk></gpx>"
                    )
                    with self.assertRaises(ValueError):
                        load_track(gpx)
