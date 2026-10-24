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


# TIFF layout: IFD0 right after the header, then the SubIFD, then the pixel strip.
IFD0_OFFSET = 8
SUBIFD_OFFSET = 200
STRIP_OFFSET = 600
WIDTH = HEIGHT = 2
BITS_PER_SAMPLE = 16
STRIP = bytes(range(WIDTH * HEIGHT * BITS_PER_SAMPLE // 8))
SHORT, LONG = 3, 4


def write_tiff(path):
    """A little-endian TIFF whose IFD0 points to one SubIFD with a 16-bit pixel strip."""
    data = bytearray(STRIP_OFFSET + len(STRIP))
    data[:IFD0_OFFSET] = struct.pack("<2sHI", b"II", 42, IFD0_OFFSET)

    def ifd(offset, entries):
        struct.pack_into("<H", data, offset, len(entries))
        for i, (tag, kind, count, value) in enumerate(sorted(entries)):
            struct.pack_into(
                "<HHII", data, offset + 2 + i * 12, tag, kind, count, value
            )
        struct.pack_into("<I", data, offset + 2 + len(entries) * 12, 0)

    ifd(IFD0_OFFSET, [(330, LONG, 1, SUBIFD_OFFSET)])  # SubIFDs
    ifd(
        SUBIFD_OFFSET,
        [
            (254, LONG, 1, 0),  # NewSubfileType
            (256, LONG, 1, WIDTH),  # ImageWidth
            (257, LONG, 1, HEIGHT),  # ImageLength
            (258, SHORT, 1, BITS_PER_SAMPLE),  # BitsPerSample
            (259, SHORT, 1, 1),  # Compression
            (262, SHORT, 1, 1),  # PhotometricInterpretation
            (273, LONG, 1, STRIP_OFFSET),  # StripOffsets
            (278, LONG, 1, HEIGHT),  # RowsPerStrip
            (279, LONG, 1, len(STRIP)),  # StripByteCounts
        ],
    )
    data[STRIP_OFFSET:] = STRIP
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
