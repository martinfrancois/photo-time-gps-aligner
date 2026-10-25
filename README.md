# Photo Time & GPS

Match RAW photos to a GPX track or Google Timeline export, correct a camera clock when needed, and optionally rename photos in capture order. Clock correction, GPS matching and renaming can be used independently.

The tool creates a compact recovery backup before editing anything. It verifies the RAW bytes, embedded previews, timestamps and GPS values as it works. A separate verification command reads the final files again.

## Requirements

- macOS or Linux, with Python 3.10 or later and the IANA timezone database. CI tests Python 3.12 on Ubuntu 24.04; macOS creation-time handling is not covered by CI.
- [ExifTool](https://exiftool.org/). On macOS, install it with `brew install exiftool`.
- RAW files with one contiguous `SubIFD` image strip. Tested with Sony ARW. Unsupported layouts stop during planning, before edits.
- Free disk space for compressed metadata backups and one temporary RAW file per worker.

Python uses only its standard library. No photos, location exports or metadata are uploaded.

## Use

### GPS matching only

If the capture times are already correct, no clock reference is needed:

```sh
python3 photo_time_gps.py plan /path/to/photos \
  --gps-only \
  --gpx /path/to/track.gpx \
  --timezone America/Chicago \
  --backup /path/to/photo-recovery
```

This preserves filenames and existing EXIF and Sony date/time tags, including their original precision. It preserves filesystem modification time and restores creation time on macOS when `SetFile` is available, within that tool's precision. Filesystem access and change times may reflect file processing. Unmatched photos retain their original file bytes.

Existing `OffsetTimeOriginal` values determine the UTC capture instant. `--timezone` is a fallback for photos without an EXIF UTC offset and does not rewrite any date tags. It may be omitted when every photo already has an offset. Missing offsets without a fallback, ambiguous daylight-saving times and nonexistent local times cause planning to stop.

### Correct the clock, optionally match GPS and rename

Take a photo of a trusted clock with unchanged camera settings. Supply its real timestamp with an explicit UTC offset:

```sh
python3 photo_time_gps.py plan /path/to/photos \
  --reference DSC01234.ARW \
  --actual '2030-07-09T22:54:33.27-05:00' \
  --timezone America/Chicago \
  --gpx /path/to/track.gpx \
  --rename \
  --backup /path/to/photo-recovery
```

Repeat `--gpx` to use multiple recordings in one run, for example `--gpx day-one.gpx --gpx day-two.gpx`. Files retain separate track segments and recorder policies. Recorded matches from any GPX take priority over Timeline and stop estimates; overlapping recorded matches use the first supplied file. The manifest records the source file for each GPX match.

Supply `--reference` and `--actual` together to enable clock correction. Omit both to preserve the clock. Omit `--gpx` when no track is available. Add `--rename` only when sequential filenames are wanted. `--gps-only` explicitly forbids clock correction and renaming; simply supplying `--gpx` without those operations has the same effect.

Planning reads and hashes every selected photo and writes its recovery backup. It does not change the photos. Use a new backup directory for each run.

### Use Google Timeline

Use `--timeline` for a Google device Timeline JSON export. It can also accompany `--gpx` as a fallback:

```sh
python3 photo_time_gps.py plan /path/to/photos \
  --gps-only \
  --timeline /path/to/timeline-export.json \
  --max-accuracy 50 \
  --backup /path/to/photo-recovery
```

This supports the device export containing `rawSignals.position` with `LatLng`, `timestamp`, `accuracyMeters` and `source`. Other export formats fail with an explanation. [Google documents how to export device Timeline data](https://support.google.com/maps/answer/14169818). The parser uses timestamped measured positions; it excludes inferred visits, activity endpoints and simplified semantic paths.

Only readings with a positive reported horizontal accuracy of at most `--max-accuracy` metres are accepted (default 50). Missing/invalid accuracy and malformed positions are excluded and counted in the log and manifest. Conflicting positions at the same timestamp are excluded; identical coordinates use the lowest reported error. Timeline allows up to 600 seconds between accepted readings by default, controlled by `--timeline-max-gap`. Use `--timeline-max-gap 60` for strict matching. GPX retains its separate 60-second `--max-gap` default. Longer Timeline interpolation is an approximate route position, especially while moving. A smaller reported error is preferable, but is not a guarantee of the phone's true position. Interpolation assumes movement along the path between the readings.

Each match records its contributing sources, sample timestamps and the larger reported endpoint error in the private manifest. This error is not written as the photo's GPS accuracy, since interpolation adds uncertainty. Altitude is omitted because the export does not specify its datum or vertical accuracy; stale altitude and accuracy tags are cleared when assigning a Timeline match. GPS-only measurements use the EXIF `GPS` processing method; fused or Wi-Fi measurements use `MANUAL` rather than claiming satellite-only positioning.

### Fill missing geotags using both sources

```sh
python3 photo_time_gps.py plan /path/to/photos \
  --gps-only --only-missing-gps \
  --gpx /path/to/track.gpx \
  --timeline /path/to/timeline-export.json \
  --backup /path/to/gps-fill-recovery
```

`--only-missing-gps` excludes photos that already contain both latitude and longitude. Those files are not backed up, rewritten or renamed by this run. It requires GPS matching without clock correction or renaming. If every photo already has coordinates, planning stops with an explanation. A run's reports describe its selected photos, so a zero-unmatched fill report means all selected missing geotags were filled.

The matching order is:

1. GPX interpolation within `--max-gap`, default 60 seconds.
2. Timeline interpolation within `--timeline-max-gap`, default 600 seconds.
3. A GPX stop estimate when the recorder and geometry support it. Timeline is **not required**.

Geo Tracker intentionally filters recorded points by movement and other settings. Its pauses can appear as one gap or several consecutive gaps; a waypoint may mark the start, middle or end of a pause. [Geo Tracker documents its recording filters](https://geo-tracker.org/en/faq). A gap alone does not establish that the phone stayed stationary.

With `--gpx-gap-policy auto` (the default), the GPX creator field must identify Geo Tracker to enable standalone stop estimates. `--gpx-gap-policy strict` disables them; `--gpx-gap-policy stationary` explicitly enables them for another recorder known to filter stationary points. Generic GPX tracks otherwise retain strict interpolation.

A stop estimate requires two points in the same segment, a gap no longer than `--stationary-max-gap` (default 3600 seconds), and both endpoints within `--stationary-radius` (default 50 metres) of their great-circle midpoint. This is a radius, so endpoints may be up to 100 metres apart. Set `--stationary-radius 0` to disable estimates. The estimate holds that midpoint fixed throughout the gap, instead of inventing movement between GPS readings. Altitude is omitted.

These are configurable tolerances for **approximate geotagging**, not guarantees or a count of actual stops. Nearby endpoints cannot rule out an unrecorded excursion or a GPS outage. The manifest records the assumption, recorder, gap, endpoint timestamps and observed radius; observed radius is not written as GPS accuracy. Unknown gaps outside these limits remain unmatched. Segment breaks and track boundaries are never bridged by stop estimates.

You can supply either GPX or Timeline alone. When both are present, recorded GPX matches take priority, Timeline fills GPX gaps, and GPX stop estimates remain available where Timeline has no usable match. Accepted Timeline observations that fall outside the proposed stop radius veto the estimate. A missing Timeline reading never prevents an otherwise eligible Geo Tracker estimate.

### Check annotated stops against a recording

The read-only audit helper compares GPX waypoints named `Stop for ~N min` with adjacent recording gaps:

```sh
python3 audit_stops.py /path/to/recording.gpx \
  --output /path/to/stop-comparison.json
```

It reports gaps on either side of each marker and exercises the same GPX-only matching function used by photo processing. Approximate waypoint labels are validation references, not exact start/end times, and are never used automatically to geotag photos. The audit also lists unmarked long gaps; these are unconfirmed rather than proven stops or movement. Keep reports private because they contain trip timestamps.

### Reuse a calibration for another trip

When the camera clock and timezone settings stayed unchanged, reuse a previous recovery manifest containing an original clock reference:

```sh
python3 photo_time_gps.py plan /path/to/earlier-trip \
  --calibration /path/to/previous-recovery/manifest.json \
  --timezone America/New_York \
  --timeline /path/to/timeline-export.json \
  --rename \
  --backup /path/to/earlier-trip-recovery
```

The script reads the original reference metadata from that manifest, computes the camera error in UTC, then converts each corrected instant to the new destination timezone. It does not read the already-corrected reference photo as an original. For example, a camera set to UTC+02 that is 92 seconds slow needs a different local-time adjustment in New York than in Chicago. Calibration reuse checks camera identity and the original EXIF UTC offset; a difference stops planning. Matching metadata cannot prove the camera clock was never adjusted, and one reference cannot estimate drift over several days.

`--calibration` replaces `--reference` and `--actual` and requires `--timezone`. Omit both location-source options if geotagging is not needed. Reused UTC calibration can cross destination daylight-saving transitions; direct local-clock calibration requires a constant destination UTC offset.

### Apply and verify

Inspect the reported clock correction and match count, then apply the saved plan:

```sh
python3 photo_time_gps.py apply --backup /path/to/photo-recovery
python3 photo_time_gps.py verify --backup /path/to/photo-recovery
```

With `--rename`, names are `DSC0001.ARW`, `DSC0002.ARW`, and so on. Width expands beyond four digits if needed. Photos with equal capture times use the Sony shutter counter when available for every photo, which preserves burst order across filename counter rollovers. Otherwise they use natural filename order. Clock correction rejects mixed detected camera identities. `--prefix`, `--digits` and `--extensions` customize selection and naming. Selection includes only matching files directly in the folder. Other files and subfolders are untouched.

`apply` uses two concurrent workers by default. Set `--workers 1` for sequential processing or choose up to eight workers for faster storage.

`apply` can be run again after interruption or completion. Its journal prevents double correction. A folder marker prevents accidentally repeating clock correction or renaming. GPS-only matching can be run again on a previously processed folder once its earlier run is complete; use a new recovery directory. Each run retains its own backup and report. Keep the marker and recovery directory until you are finished with the work. Do not run concurrent commands against the same folder or modify photos during a run.

## Time and GPS accuracy

The offset is the difference between the reference photo's stored camera time and the supplied real local time. When clock correction is enabled, the script corrects EXIF capture, digitization and modification dates, Sony capture time when present, fractional seconds, UTC offsets and filesystem modification time. It preserves the original date/time values in the recovery manifest. On macOS, creation time is also corrected when the `SetFile` tool is available. Original creation times are saved for restoration. Other platforms retain their native creation-time behavior.

A single reference measures a constant clock offset. It cannot measure clock drift or recover subsecond capture times the camera did not record. A reference timestamp with hundredths does not make every other photo accurate to a hundredth. Use separate runs if the camera's clock settings changed. Direct reference clock correction rejects photos spanning a destination daylight-saving offset change. GPS-only matching can cross such transitions when the capture instants are unambiguous.

GPX times are read with their UTC offsets. Positions are interpolated along a great-circle path between adjacent track points. Altitude is interpolated when both points have it. Separate track segments are never joined. Exact timestamp matches use the recorded point. There is no extrapolation before or after coverage.

The default maximum GPX gap is 60 seconds. Change it with `--max-gap`, but longer gaps can produce misleading locations, especially while traveling. Photos outside accepted coverage retain their original GPS tags. The manifest records each match's interpolation gap; `verification.json` lists unmatched filenames. Each completed plan, apply and verification also writes a UTC-timestamped `gps-unmatched-<timestamp>-<phase>.txt` file in the recovery directory. It lists final and original filenames, capture time used for matching, and the reason each photo could not be matched. A report is written even if every photo matched. Accuracy also depends on phone reception, its distance from the camera, and movement between track points. The script does not invent GPS accuracy estimates.

## Backup and recovery

Each backup stores every original byte except the large RAW image strip. It records SHA-256 hashes of the entire original file, the RAW strip and embedded previews. Restoration combines the saved bytes with the unchanged RAW strip and must match the original full-file SHA-256.

This is a compact **metadata-edit recovery backup**, not an independent backup of your photos. Keep a separate photo backup to protect against drive loss, deletion or later corruption of the RAW data. Recovery files include private metadata and embedded previews; keep them private.

Restore original names, original file bytes and modification times to a separate directory:

```sh
python3 photo_time_gps.py restore \
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
python3 -m compileall -q photo_time_gps.py
# Optional style checks in a development virtual environment:
python3 -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
```

Tests use synthetic data. With ExifTool installed, the integration tests exercise GPS-only planning, Timeline filtering, calibration reuse across timezones, renaming, editing, repeat runs and byte-exact restoration using generated TIFF RAW fixtures. For integration testing, work on copies of your own RAW files, run all four commands, and compare restored files byte for byte with the originals. Never commit photo samples, real tracks or Timeline exports.

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under MIT.

## Sony RAW layout warnings

ExifTool may log `[minor] Oversized SubIFD StripByteCounts` for some Sony ARW files. Its author identifies this as a [known ARW issue](https://exiftool.org/forum/index.php?topic=17663.0). The script keeps the warning in the log and still requires unchanged RAW and preview hashes, preserved unrelated metadata, and byte-exact reconstruction of the original. Investigate any new or different warning rather than suppressing warnings globally.

## Earlier versions

The project was previously named Photo clock fix. `photo_clock_fix.py` remains a compatibility entry point for the new CLI. Existing recovery manifests and folder markers remain readable. Renaming is now opt-in with `--rename`, including through the compatibility entry point. Archived scripts in existing recovery folders are left unchanged.
