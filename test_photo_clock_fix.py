import datetime as dt
from pathlib import Path
import tempfile
import argparse
from unittest.mock import patch
import unittest

import photo_clock_fix as fix


class ClockTests(unittest.TestCase):
    def test_clock_offset_rolls_date(self):
        source = fix.captured({"ExifIFD:DateTimeOriginal": "2030:07:10 05:53:01"})
        actual = dt.datetime.fromisoformat("2030-07-09T22:54:33.27-05:00")
        offset = actual.replace(tzinfo=None) - source
        self.assertEqual(offset.total_seconds(), -25107.73)
        self.assertEqual(source + offset, actual.replace(tzinfo=None))

    def test_missing_fraction_and_leading_zero_fraction(self):
        self.assertEqual(
            fix.captured(
                {
                    "ExifIFD:DateTimeOriginal": "2030:01:01 00:00:00",
                    "ExifIFD:SubSecTimeOriginal": "0012",
                }
            ).microsecond,
            1200,
        )

    def test_gps_no_extrapolation_or_gap_crossing(self):
        track = [[(0, 0, 0, 10), (20, 0, 2, 30)]]

        def when(x):
            return dt.datetime.fromtimestamp(x, fix.UTC)

        self.assertIsNone(fix.position(track, when(-1), 60))
        self.assertIsNone(fix.position(track, when(21), 60))
        self.assertIsNone(fix.position(track, when(10), 5))
        p = fix.position(track, when(10), 60)
        self.assertAlmostEqual(p["lon"], 1)
        self.assertEqual(p["altitude"], 20)
        self.assertEqual(fix.position(track, when(0), 1)["method"], "exact")

    def test_segments_not_joined(self):
        self.assertIsNone(
            fix.position(
                [[(0, 0, 0, None)], [(20, 0, 0, None)]],
                dt.datetime.fromtimestamp(10, fix.UTC),
                60,
            )
        )

    def test_antimeridian(self):
        p = fix.position(
            [[(0, 0, 179, None), (20, 0, -179, None)]],
            dt.datetime.fromtimestamp(10, fix.UTC),
            60,
        )
        self.assertAlmostEqual(abs(p["lon"]), 180)

    def test_unmatched_reasons_and_timestamped_report(self):
        def when(x):
            return dt.datetime.fromtimestamp(x, fix.UTC)

        track = [[(10, 0, 0, None), (100, 0, 0, None)]]
        self.assertIn("No GPX", fix.unmatched_reason([], when(20), 60))
        self.assertIn("Before", fix.unmatched_reason(track, when(0), 60))
        self.assertIn("After", fix.unmatched_reason(track, when(110), 60))
        self.assertIn("90.00 seconds", fix.unmatched_reason(track, when(20), 60))
        self.assertIn(
            "separate", fix.unmatched_reason([track[0][:1], track[0][1:]], when(20), 60)
        )
        with tempfile.TemporaryDirectory() as directory:
            record = dict(
                target="DSC0001.ARW",
                original="DSC0123.ARW",
                corrected=when(0).isoformat(),
                gps=None,
                gps_reason="Before first GPS point.",
            )
            path = Path(
                fix.unmatched_report(Path(directory), {"records": [record]}, "verified")
            )
            self.assertRegex(
                path.name, r"gps-unmatched-\d{8}T\d{6}\.\d{6}Z-verified.txt"
            )
            self.assertIn("DSC0001.ARW", path.read_text())
            self.assertIn("Before first GPS point.", path.read_text())

    def test_plan_resume_and_shutter_order_through_rollover(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve() / "photos"
            folder.mkdir()
            backup = Path(directory) / "backup"
            metas = []
            for name, count in [("DSC09999.ARW", 12), ("DSC00001.ARW", 13)]:
                path = folder / name
                path.write_bytes(b"header" + b"raw" * 100)
                metas.append(
                    {
                        "SourceFile": str(path),
                        "ExifIFD:DateTimeOriginal": "2030:07:10 01:00:00",
                        "System:FileSize": path.stat().st_size,
                        "System:FileModifyDate": dt.datetime.fromtimestamp(
                            path.stat().st_mtime, fix.UTC
                        ).strftime("%Y:%m:%d %H:%M:%S%z"),
                        "SubIFD:StripOffsets": 6,
                        "SubIFD:StripByteCounts": 300,
                        "Sony:ShutterCount": count,
                    }
                )
            args = argparse.Namespace(
                folder=folder,
                backup=backup,
                reference=Path("DSC09999.ARW"),
                actual="2030-07-09T18:00:00-05:00",
                timezone="America/Chicago",
                gpx=None,
                extensions="ARW",
                prefix="DSC",
                digits=4,
                max_gap=60,
                resume=False,
            )
            real_backup = fix.backup_one
            calls = []

            def interrupted(path, meta, dest):
                calls.append(path)
                if len(calls) == 2:
                    raise OSError("simulated interruption")
                return real_backup(path, meta, dest)

            with (
                patch.object(fix, "exif", return_value=metas),
                patch.object(fix.subprocess, "check_output", return_value="test"),
                patch.object(fix, "backup_one", side_effect=interrupted),
            ):
                with self.assertRaisesRegex(OSError, "interruption"):
                    fix.plan(args)
            args.resume = True
            with (
                patch.object(
                    fix,
                    "exif",
                    side_effect=AssertionError("resume should reuse saved metadata"),
                ),
                patch.object(fix.subprocess, "check_output", return_value="test"),
            ):
                fix.plan(args)
            manifest = fix.load(backup)
            self.assertEqual(
                [r["original"] for r in manifest["records"]],
                ["DSC09999.ARW", "DSC00001.ARW"],
            )
            for i, r in enumerate(manifest["records"]):
                fix.reconstructed(backup, i, r, folder / r["original"], r["metadata"])
            self.assertEqual(len(list(backup.glob("gps-unmatched-*.txt"))), 1)

    def test_backup_reconstruction_and_corruption_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "bytes").mkdir()
            original = folder / "original"
            original.write_bytes(b"HEADER" + b"raw" * 100 + b"TAIL")
            meta = {"SubIFD:StripOffsets": 6, "SubIFD:StripByteCounts": 300}
            backup = fix.backup_one(original, meta, folder / "bytes/0.gz")
            edited = folder / "edited"
            edited.write_bytes(b"NEW HEADER" + b"raw" * 100 + b"CHANGED TAIL")
            new_meta = {"SubIFD:StripOffsets": 10, "SubIFD:StripByteCounts": 300}
            output = folder / "restored"
            with output.open("wb") as f:
                fix.reconstructed(folder, 0, {"backup": backup}, edited, new_meta, f)
            self.assertEqual(output.read_bytes(), original.read_bytes())
            edited.write_bytes(b"NEW HEADER" + b"bad" * 100 + b"CHANGED TAIL")
            with self.assertRaisesRegex(ValueError, "integrity"):
                fix.reconstructed(folder, 0, {"backup": backup}, edited, new_meta)


if __name__ == "__main__":
    unittest.main()
