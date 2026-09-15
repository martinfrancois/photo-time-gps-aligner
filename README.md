# Photo clock fix

Correct a camera's clock from a photo of a trusted clock, rename RAW photos in capture order, and optionally geotag them from a phone's GPX recording.

The tool creates a compact recovery backup before editing anything. It verifies the RAW bytes, embedded previews, timestamps and GPS values as it works. A separate verification command reads the final files again.

## Requirements

- macOS or Linux, with Python 3.10 or later and the IANA timezone database.
- [ExifTool](https://exiftool.org/). On macOS, install it with `brew install exiftool`.
- RAW files with one contiguous `SubIFD` image strip. Tested with Sony ARW. Unsupported layouts stop during planning, before edits.
- Free disk space for compressed metadata backups and one temporary RAW file per worker.

Python uses only its standard library. No photos, GPX tracks or metadata are uploaded.

## Use

Take a photo of a trusted clock with the same camera clock settings as your other photos. Supply the timestamp visible in that photo, including the date, fractional seconds if available, and UTC offset. Use an IANA timezone name for the destination timezone.

```sh
python3 photo_clock_fix.py plan /path/to/photos \
  --reference DSC01234.ARW \
  --actual '2030-07-09T22:54:33.27-05:00' \
  --timezone America/Chicago \
  --gpx /path/to/track.gpx \
  --backup /path/to/photo-recovery
```

Omit `--gpx` when you have no track. This leaves existing GPS tags unchanged. Planning reads and hashes every photo and writes the recovery backup. It does not change the photos. The backup folder must be new.

Inspect the reported clock correction and match count, then apply the saved plan:

```sh
python3 photo_clock_fix.py apply --backup /path/to/photo-recovery
python3 photo_clock_fix.py verify --backup /path/to/photo-recovery
```

Default names are `DSC0001.ARW`, `DSC0002.ARW`, and so on. Width expands beyond four digits if needed. Photos with equal capture times use the Sony shutter counter when available for every photo, which preserves burst order across filename counter rollovers. Otherwise they use natural filename order. Mixed detected camera identities are rejected. `--prefix`, `--digits` and `--extensions` customize selection and naming. Selection includes only matching files directly in the folder. Other files and subfolders are untouched.

`apply` uses two concurrent workers by default. Set `--workers 1` for sequential processing or choose up to eight workers for faster storage.

`apply` can be run again after interruption or completion. Its journal prevents double correction. A folder marker prevents accidentally planning another correction on processed photos. Keep the marker and recovery directory until you are finished with the work. Do not run concurrent commands against the same folder or modify photos during a run.

## Time and GPS accuracy

The offset is the difference between the reference photo's stored camera time and the supplied real local time. The script corrects EXIF capture, digitization and modification dates, Sony capture time when present, fractional seconds, UTC offsets and filesystem modification time. It preserves the original date/time values in the recovery manifest. On macOS, creation time is also corrected when the `SetFile` tool is available. Original creation times are saved for restoration. Other platforms retain their native creation-time behavior.

A single reference measures a constant clock offset. It cannot measure clock drift or recover subsecond capture times the camera did not record. A reference timestamp with hundredths does not make every other photo accurate to a hundredth. Use separate runs if the camera's clock settings changed. The script rejects photos spanning a destination daylight-saving offset change.

GPX times are read with their UTC offsets. Positions are interpolated along a great-circle path between adjacent track points. Altitude is interpolated when both points have it. Separate track segments are never joined. Exact timestamp matches use the recorded point. There is no extrapolation before or after coverage.

The default maximum gap is 60 seconds. Change it with `--max-gap`, but longer gaps can produce misleading locations, especially while traveling. Photos outside accepted coverage retain their original GPS tags. The manifest records each match's interpolation gap; `verification.json` lists unmatched filenames. Each completed plan, apply and verification also writes a UTC-timestamped `gps-unmatched-<timestamp>-<phase>.txt` file in the recovery directory. It lists final and original filenames, corrected capture time, and the reason each photo could not be matched. A report is written even if every photo matched. Accuracy also depends on phone reception, its distance from the camera, and movement between track points. The script does not invent GPS accuracy estimates.

## Backup and recovery

Each backup stores every original byte except the large RAW image strip. It records SHA-256 hashes of the entire original file, the RAW strip and embedded previews. Restoration combines the saved bytes with the unchanged RAW strip and must match the original full-file SHA-256.

This is a compact **metadata-edit recovery backup**, not an independent backup of your photos. Keep a separate photo backup to protect against drive loss, deletion or later corruption of the RAW data. Recovery files include private metadata and embedded previews; keep them private.

Restore original names, original file bytes and modification times to a separate directory:

```sh
python3 photo_clock_fix.py restore \
  --backup /path/to/photo-recovery \
  --output /path/to/restored-originals
```

Restoring needs space for complete original photos. It verifies every reconstructed file before giving it its original name. Review the restored directory before replacing processed photos. Extended attributes and unrelated sidecar files are outside the restoration scope. Original filesystem creation times are restored on macOS when `SetFile` is available, to the precision supported by that tool.

The run directory contains:

- `manifest.json`: original metadata, hashes, filename mapping and planned results.
- `bytes/`: compressed original non-RAW bytes.
- `state.json`: apply/resume journal.
- `exiftool.log`: metadata writer output.
- `verification.json`: final verification results.
- `gps-unmatched-*.txt`: timestamped unmatched-file reports with reasons.

Timestamped operation logs sit beside the recovery directory, so planning failures are logged even before that directory exists. They record arguments, software versions, per-file progress, completion status and full error tracebacks. `exiftool.log` includes timestamps, filenames, return codes, warnings and writer output. Logs contain private local paths and metadata; keep them out of public repositories.

Do not delete an incomplete backup and assume work succeeded. If planning is interrupted, photos are untouched. Repeat the same `plan` command with `--resume` to reuse completed backups. The script checks the inventory, sizes and modification times before reusing saved metadata. It can also start a new plan in a new backup directory. If applying is interrupted, resume using the same backup directory.

## Development

```sh
python3 -m unittest -v
python3 -m compileall -q photo_clock_fix.py
# Optional style checks in a development virtual environment:
python3 -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
```

Tests use synthetic data. For integration testing, work on copies of your own RAW files, run all four commands, and compare restored files byte for byte with the originals. Never commit photo samples or real tracks.

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under MIT.
