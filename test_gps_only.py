import copy
import datetime as dt
import unittest

import photo_time_gps as app


class GPSOnlyTests(unittest.TestCase):
    def test_existing_offset_takes_precedence_over_fallback(self):
        meta = {
            "ExifIFD:DateTimeOriginal": "2030:07:09 20:00:00",
            "ExifIFD:OffsetTimeOriginal": "+02:00",
            "ExifIFD:SubSecTimeOriginal": "125",
        }
        self.assertEqual(
            app.capture_instant(meta, "America/Chicago")
            .astimezone(app.UTC)
            .isoformat(),
            "2030-07-09T18:00:00.125000+00:00",
        )

    def test_missing_offset_requires_timezone(self):
        meta = {"ExifIFD:DateTimeOriginal": "2030:07:09 20:00:00"}
        with self.assertRaisesRegex(ValueError, "--timezone"):
            app.capture_instant(meta)
        self.assertEqual(
            app.capture_instant(meta, "America/Chicago").utcoffset(),
            dt.timedelta(hours=-5),
        )

    def test_ambiguous_and_nonexistent_local_times_rejected(self):
        for stamp in ["2026:11:01 01:30:00", "2026:03:08 02:30:00"]:
            with self.assertRaisesRegex(ValueError, "ambiguous or nonexistent"):
                app.capture_instant(
                    {"ExifIFD:DateTimeOriginal": stamp}, "America/Chicago"
                )

    def test_gps_only_writes_no_capture_date_tags(self):
        record = {
            "correct_time": False,
            "corrected": "2030-07-09T20:00:00-05:00",
            "metadata": {},
            "gps": {"lat": 40, "lon": -80, "altitude": None},
        }
        edits = app.write_args(record)
        self.assertTrue(edits)
        self.assertTrue(all(arg.startswith("-GPS") for arg in edits))
        record["gps"] = None
        self.assertEqual(app.write_args(record), [])

    def test_gps_only_verifier_rejects_added_or_changed_timestamps(self):
        meta = {
            "ExifIFD:DateTimeOriginal": "2030:07:09 20:00:00",
            "IFD0:ModifyDate": "2030:07:10 12:00:00",
        }
        record = {
            "correct_time": False,
            "corrected": "2030-07-09T20:00:00-05:00",
            "metadata": meta,
            "gps": None,
        }
        app.check_metadata(record, meta)
        for tag, value in [
            ("ExifIFD:DateTimeOriginal", "2030:07:09 19:00:00"),
            ("ExifIFD:SubSecTimeOriginal", "000000"),
            ("ExifIFD:OffsetTimeOriginal", "-05:00"),
        ]:
            changed = copy.copy(meta)
            changed[tag] = value
            with self.assertRaisesRegex(ValueError, "Unrelated metadata changed"):
                app.check_metadata(record, changed)

    def test_legacy_plan_still_writes_time_tags(self):
        record = {"corrected": "2030-07-09T20:00:00-05:00", "metadata": {}, "gps": None}
        self.assertIn(
            "-ExifIFD:DateTimeOriginal=2030:07:09 20:00:00", app.write_args(record)
        )


if __name__ == "__main__":
    unittest.main()
