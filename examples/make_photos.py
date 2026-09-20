#!/usr/bin/env python3
"""Write three tiny synthetic RAW files for the README example. Requires ExifTool."""

import argparse
from pathlib import Path
import shutil
import struct
import subprocess
import sys

# Camera time of each photo. The README example treats this clock as 90 seconds fast.
PHOTOS = {
    "DSC00010.ARW": "2030:07:09 20:00:00",
    "DSC00011.ARW": "2030:07:09 20:00:30",
    "DSC00012.ARW": "2030:07:09 20:05:00",
}
CAMERA_OFFSET = "-05:00"


def write_tiff(path):
    """A little-endian TIFF whose IFD0 points to one SubIFD with a 2x2 16-bit strip."""
    data = bytearray(608)
    data[:8] = struct.pack("<2sHI", b"II", 42, 8)

    def ifd(offset, entries):
        struct.pack_into("<H", data, offset, len(entries))
        for i, (tag, kind, count, value) in enumerate(sorted(entries)):
            struct.pack_into(
                "<HHII", data, offset + 2 + i * 12, tag, kind, count, value
            )
        struct.pack_into("<I", data, offset + 2 + len(entries) * 12, 0)

    ifd(8, [(330, 4, 1, 200)])  # SubIFDs
    ifd(
        200,
        [
            (254, 4, 1, 0),  # NewSubfileType
            (256, 4, 1, 2),  # ImageWidth
            (257, 4, 1, 2),  # ImageLength
            (258, 3, 1, 16),  # BitsPerSample
            (259, 3, 1, 1),  # Compression
            (262, 3, 1, 1),  # PhotometricInterpretation
            (273, 4, 1, 600),  # StripOffsets
            (278, 4, 1, 2),  # RowsPerStrip
            (279, 4, 1, 8),  # StripByteCounts
        ],
    )
    data[600:] = bytes(range(8))
    path.write_bytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "folder", type=Path, help="Destination folder, created if missing"
    )
    args = parser.parse_args()
    if not shutil.which("exiftool"):
        sys.exit("Install ExifTool first: https://exiftool.org/")
    args.folder.mkdir(parents=True, exist_ok=True)
    for name, stamp in PHOTOS.items():
        path = args.folder / name
        write_tiff(path)
        subprocess.run(
            [
                "exiftool",
                "-overwrite_original",
                f"-DateTimeOriginal={stamp}",
                f"-OffsetTimeOriginal={CAMERA_OFFSET}",
                str(path),
            ],
            check=True,
            capture_output=True,
        )
        print(f"{path}: camera time {stamp} {CAMERA_OFFSET}")


if __name__ == "__main__":
    main()
