"""Synthetic location data only."""

import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import photo_time_gps as app
from test_integration import synthetic_raw
from timeline import Timeline


def signal(stamp, accuracy=4, point="10°, 20°", source="GPS"):
    return {
        "position": {
            "timestamp": stamp,
            "LatLng": point,
            "accuracyMeters": accuracy,
            "source": source,
            "altitudeMeters": 999,
        }
    }


class TimelineTests(unittest.TestCase):
    def test_accuracy_duplicates_timezone_and_gap_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text(
                json.dumps(
                    {
                        "rawSignals": [
                            signal("2030-07-09T12:00:00-04:00"),
                            signal(
                                "2030-07-09T16:00:20Z",
                                point="10°, 20.001°",
                                source="WIFI",
                            ),
                            signal("2030-07-09T16:00:30Z", accuracy=100),
                            signal("2030-07-09T16:00:40Z", point="11°, 20°"),
                            signal("2030-07-09T16:00:40Z", point="12°, 20°"),
                            signal("bad"),
                            signal("2030-07-09T16:10:00Z"),
                        ],
                        "semanticSegments": [{"visit": {"deliberately": "ignored"}}],
                    }
                )
            )
            track = Timeline(path)
            self.assertEqual(track.stats["accepted"], 3)
            self.assertEqual(track.stats["inaccurate_or_missing_accuracy"], 1)
            self.assertEqual(track.stats["conflicting_timestamps"], 1)
            self.assertEqual(track.stats["malformed"], 1)

            def match(stamp):
                return track.match(
                    dt.datetime.fromisoformat(stamp),
                    60,
                    app.position,
                    app.unmatched_reason,
                )

            result, reason = match("2030-07-09T16:00:10+00:00")
            self.assertIsNone(reason)
            self.assertAlmostEqual(result["lon"], 20.0005, places=6)
            self.assertEqual(result["sources"], ["GPS", "WIFI"])
            self.assertEqual(result["processing_method"], "MANUAL")
            self.assertIsNone(result["altitude"])
            self.assertIsNone(match("2030-07-09T15:59:59+00:00")[0])
            self.assertIsNone(match("2030-07-09T16:01:00+00:00")[0])

    def test_unsupported_export_and_no_acceptable_points_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            for data in [
                {"semanticSegments": []},
                {"rawSignals": []},
                {"rawSignals": [signal("2030-07-09T16:00:00")]},
            ]:
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    Timeline(path)

    @unittest.skipUnless(shutil.which("exiftool"), "ExifTool required")
    def test_reuse_calibration_in_different_zone_timeline_rename_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            photos = root / "photos"
            photos.mkdir()
            original = photos / "old-name.ARW"
            synthetic_raw(original, "2030:07:09 21:00:00")
            subprocess.run(
                [
                    "exiftool",
                    "-overwrite_original",
                    "-OffsetTimeOriginal=+02:00",
                    str(original),
                ],
                check=True,
                capture_output=True,
            )
            original_bytes = original.read_bytes()
            metadata = app.exif([original], True)[0]
            calibration = root / "calibration.json"
            calibration.write_text(
                json.dumps(
                    {
                        "ready": True,
                        "reference": "reference.ARW",
                        "actual": "2030-07-09T14:01:32.27-05:00",
                        "records": [
                            {"original": "reference.ARW", "metadata": metadata}
                        ],
                    }
                )
            )
            timeline = root / "synthetic.json"
            timeline.write_text(
                json.dumps(
                    {
                        "rawSignals": [
                            signal("2030-07-09T19:01:30Z"),
                            signal("2030-07-09T19:01:35Z"),
                        ]
                    }
                )
            )
            backup = root / "backup"

            def cli(*args, success=True):
                result = subprocess.run(
                    [sys.executable, app.__file__, *map(str, args)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    result.returncode == 0, success, result.stdout + result.stderr
                )

            cli(
                "plan",
                photos,
                "--calibration",
                calibration,
                "--timezone",
                "America/New_York",
                "--timeline",
                timeline,
                "--rename",
                "--backup",
                backup,
            )
            manifest = app.load(backup)
            self.assertEqual(manifest["utc_correction_seconds"], 92.27)
            self.assertEqual(
                manifest["records"][0]["corrected"], "2030-07-09T15:01:32.270000-04:00"
            )
            cli("apply", "--backup", backup)
            cli("verify", "--backup", backup)
            self.assertEqual(
                json.loads((backup / "verification.json").read_text())["failures"], []
            )
            cli("restore", "--backup", backup, "--output", root / "restored")
            self.assertEqual(
                (root / "restored" / "old-name.ARW").read_bytes(), original_bytes
            )
            cli(
                "plan",
                photos,
                "--gps-only",
                "--timeline",
                timeline,
                "--backup",
                root / "gps-only",
            )
            cli("apply", "--backup", root / "gps-only")
            cli("verify", "--backup", root / "gps-only")
            # Changed camera timezone must not silently reuse the calibration.
            other = root / "other"
            other.mkdir()
            synthetic_raw(other / "different.ARW", "2030:07:09 21:00:00")
            cli(
                "plan",
                other,
                "--calibration",
                calibration,
                "--timezone",
                "America/New_York",
                "--backup",
                root / "rejected",
                success=False,
            )
