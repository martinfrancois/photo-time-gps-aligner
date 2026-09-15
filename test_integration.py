"""End-to-end GPS-only checks using synthetic TIFF RAW bytes, never personal media."""

import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

import photo_time_gps as app


def synthetic_raw(path, stamp):
    data = bytearray(608)
    data[:8] = struct.pack("<2sHI", b"II", 42, 8)

    def ifd(offset, entries):
        struct.pack_into("<H", data, offset, len(entries))
        for i, (tag, kind, count, value) in enumerate(sorted(entries)):
            struct.pack_into(
                "<HHII", data, offset + 2 + i * 12, tag, kind, count, value
            )
        struct.pack_into("<I", data, offset + 2 + len(entries) * 12, 0)

    ifd(8, [(330, 4, 1, 200)])
    ifd(
        200,
        [
            (254, 4, 1, 0),
            (256, 4, 1, 2),
            (257, 4, 1, 2),
            (258, 3, 1, 16),
            (259, 3, 1, 1),
            (262, 3, 1, 1),
            (273, 4, 1, 600),
            (278, 4, 1, 2),
            (279, 4, 1, 8),
        ],
    )
    data[600:] = bytes(range(8))
    path.write_bytes(data)
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            f"-DateTimeOriginal={stamp}",
            "-OffsetTimeOriginal=-05:00",
            str(path),
        ],
        capture_output=True,
        check=True,
    )


@unittest.skipUnless(shutil.which("exiftool"), "ExifTool required for integration test")
class IntegrationTests(unittest.TestCase):
    def test_gps_only_preserves_dates_names_and_unmatched_bytes_and_can_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            photos = root / "photos"
            photos.mkdir()
            synthetic_raw(photos / "keep-this-name.ARW", "2030:07:09 20:00:00")
            synthetic_raw(photos / "unmatched.ARW", "2030:07:09 21:00:00")
            originals = {p.name: p.read_bytes() for p in photos.iterdir()}
            times = {p.name: p.stat().st_mtime_ns for p in photos.iterdir()}
            gpx = root / "track.gpx"
            gpx.write_text(
                '<gpx><trk><trkseg><trkpt lat="10" lon="20"><time>2030-07-10T00:59:59Z</time></trkpt><trkpt lat="10" lon="20"><time>2030-07-10T01:00:01Z</time></trkpt></trkseg></trk></gpx>'
            )

            def cli(*args):
                result = subprocess.run(
                    [sys.executable, str(Path(app.__file__)), *map(str, args)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            for name in ["first-backup", "second-backup"]:
                backup = root / name
                cli("plan", photos, "--gps-only", "--gpx", gpx, "--backup", backup)
                manifest = app.load(backup)
                self.assertFalse(manifest["correct_time"])
                self.assertFalse(manifest["rename"])
                self.assertIsNone(manifest["reference"])
                cli("apply", "--backup", backup)
                cli("verify", "--backup", backup)
                result = json.loads((backup / "verification.json").read_text())
                self.assertEqual(result["checked"], 2)
                self.assertEqual(result["gps_matched"], 1)
                self.assertEqual(result["failures"], [])
                self.assertEqual(
                    set(p.name for p in photos.glob("*.ARW")), set(originals)
                )
                for p in photos.glob("*.ARW"):
                    self.assertAlmostEqual(
                        p.stat().st_mtime_ns / 1e9, times[p.name] / 1e9, places=4
                    )
                self.assertEqual(
                    (photos / "unmatched.ARW").read_bytes(), originals["unmatched.ARW"]
                )
                self.assertTrue(Path(result["unmatched_report"]).exists())
            cli(
                "restore",
                "--backup",
                root / "first-backup",
                "--output",
                root / "restored",
            )
            for name, data in originals.items():
                self.assertEqual((root / "restored" / name).read_bytes(), data)
