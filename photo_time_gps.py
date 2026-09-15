#!/usr/bin/env python3
"""Correct RAW photo times, match GPX or Google Timeline locations, and optionally rename photos."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import bisect
import datetime as dt
import gzip
import hashlib
import json
import logging
import platform
import fcntl
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
from timeline import Timeline
from matching import match as match_locations

UTC = dt.timezone.utc
CHUNK = 8 * 1024 * 1024


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w") as f:
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def exif(paths, all_tags=False):
    args = ["exiftool", "-j", "-G1", "-s", "-n", "-fast"]
    if not all_tags:
        args += [
            "-DateTimeOriginal",
            "-SubSecTimeOriginal",
            "-StripOffsets",
            "-StripByteCounts",
            "-PreviewImageStart",
            "-PreviewImageLength",
            "-ThumbnailOffset",
            "-ThumbnailLength",
            "-time:all",
            "-GPS:all",
        ]
    # Argument file avoids operating-system command length limits.
    if any("\n" in str(p) or "\r" in str(p) for p in paths):
        raise ValueError("Newlines in paths are unsupported")
    result = subprocess.run(
        args + ["-@", "-"],
        input="\n".join(map(str, paths)) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def captured(meta):
    value = meta["ExifIFD:DateTimeOriginal"]
    result = dt.datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    fraction = str(meta.get("ExifIFD:SubSecTimeOriginal", "0"))
    return result.replace(microsecond=int((fraction + "000000")[:6]))


def strip(meta, size):
    # Compact, byte-exact backup for TIFF RAWs with one contiguous raw strip.
    # Other layouts are backed up in full rather than guessed.
    offset = meta.get("SubIFD:StripOffsets")
    length = meta.get("SubIFD:StripByteCounts")
    if (
        isinstance(offset, int)
        and isinstance(length, int)
        and 0 < offset < size
        and 0 < length <= size - offset
    ):
        return offset, length
    return 0, 0


def hash_region(path, offset, length):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        f.seek(offset)
        while length:
            data = f.read(min(CHUNK, length))
            if not data:
                raise ValueError(f"Truncated file: {path}")
            digest.update(data)
            length -= len(data)
    return digest.hexdigest()


def backup_one(path, meta, destination):
    st = path.stat()
    offset, length = strip(meta, st.st_size)
    whole, raw = hashlib.sha256(), hashlib.sha256()
    with path.open("rb") as f, gzip.open(destination, "wb", compresslevel=1) as out:
        # Store every original byte except the unchanged raw strip.
        for count, is_raw in [
            (offset, False),
            (length, True),
            (st.st_size - offset - length, False),
        ]:
            while count:
                data = f.read(min(CHUNK, count))
                if not data:
                    raise ValueError(f"Truncated file: {path}")
                whole.update(data)
                if is_raw:
                    raw.update(data)
                else:
                    out.write(data)
                count -= len(data)
    with destination.open("rb") as saved:
        os.fsync(saved.fileno())
    previews = {}
    for label, start, size in [
        ("preview", "IFD0:PreviewImageStart", "IFD0:PreviewImageLength"),
        ("thumbnail", "IFD1:ThumbnailOffset", "IFD1:ThumbnailLength"),
    ]:
        if start in meta and size in meta:
            previews[label] = dict(
                start_tag=start,
                size_tag=size,
                length=meta[size],
                sha256=hash_region(path, meta[start], meta[size]),
            )
    return dict(
        previews=previews,
        size=st.st_size,
        offset=offset,
        length=length,
        sha256=whole.hexdigest(),
        raw_sha256=raw.hexdigest(),
        birthtime=getattr(st, "st_birthtime", None),
        mtime_ns=st.st_mtime_ns,
        atime_ns=st.st_atime_ns,
        mode=st.st_mode,
    )


def load_track(path):
    tracks = []
    root = ET.parse(path).getroot()
    for segment in root.findall(".//{*}trkseg"):
        points = []
        for p in segment.findall("{*}trkpt"):
            t = p.find("{*}time")
            if t is None or not t.text:
                continue
            instant = dt.datetime.fromisoformat(t.text.replace("Z", "+00:00"))
            if instant.utcoffset() is None:
                raise ValueError("GPX timestamps must include UTC offsets")
            lat, lon = float(p.attrib["lat"]), float(p.attrib["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("Invalid GPX coordinates")
            ele = p.find("{*}ele")
            points.append(
                (
                    instant.timestamp(),
                    lat,
                    lon,
                    float(ele.text) if ele is not None else None,
                )
            )
        points.sort()
        if any(a[0] == b[0] and a[1:] != b[1:] for a, b in zip(points, points[1:])):
            raise ValueError("Conflicting GPX points at the same time")
        if points:
            tracks.append(points)
    if not tracks:
        raise ValueError("GPX contains no timestamped track points")
    return tracks


def position(tracks, instant, max_gap):
    t = instant.timestamp()
    for points in tracks:
        times = [p[0] for p in points]
        i = bisect.bisect_left(times, t)
        if i < len(points) and times[i] == t:
            return dict(
                lat=points[i][1],
                lon=points[i][2],
                altitude=points[i][3],
                gap=0,
                method="exact",
            )
        if not 0 < i < len(points):
            continue
        a, b = points[i - 1], points[i]
        gap = b[0] - a[0]
        if gap > max_gap:
            continue
        fraction = (t - a[0]) / gap

        # Great-circle interpolation also handles the antimeridian.
        def vector(p):
            lat, lon = map(math.radians, p[1:3])
            return [
                math.cos(lat) * math.cos(lon),
                math.cos(lat) * math.sin(lon),
                math.sin(lat),
            ]

        u, v = vector(a), vector(b)
        angle = math.acos(max(-1, min(1, sum(x * y for x, y in zip(u, v)))))
        if angle < 1e-10:
            w = u
        else:
            w = [
                (math.sin((1 - fraction) * angle) * x + math.sin(fraction * angle) * y)
                / math.sin(angle)
                for x, y in zip(u, v)
            ]
        lat = math.degrees(math.atan2(w[2], math.hypot(w[0], w[1])))
        lon = math.degrees(math.atan2(w[1], w[0]))
        altitude = (
            a[3] + fraction * (b[3] - a[3])
            if a[3] is not None and b[3] is not None
            else None
        )
        return dict(lat=lat, lon=lon, altitude=altitude, gap=gap, method="interpolated")
    return None


def unmatched_reason(tracks, instant, max_gap):
    if not tracks:
        return "No location track supplied."
    t = instant.timestamp()
    first = min(p[0][0] for p in tracks)
    last = max(p[-1][0] for p in tracks)
    if t < first:
        return f"Before first GPS track point by {first - t:.2f} seconds."
    if t > last:
        return f"After last GPS track point by {t - last:.2f} seconds."
    for points in tracks:
        times = [p[0] for p in points]
        i = bisect.bisect_left(times, t)
        if 0 < i < len(times):
            gap = times[i] - times[i - 1]
            if gap > max_gap:
                before = dt.datetime.fromtimestamp(times[i - 1], UTC).isoformat()
                after = dt.datetime.fromtimestamp(times[i], UTC).isoformat()
                return f"GPS gap of {gap:.2f} seconds exceeds {max_gap:g}-second limit; bracketing points {before} and {after}."
    return "Between separate GPS track segments; interpolation is disabled."


def unmatched_report(backup, manifest, phase):
    stamp = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output = backup / f"gps-unmatched-{stamp}-{phase}.txt"
    unmatched = [r for r in manifest["records"] if r["gps"] is None]
    lines = [
        f"GPS unmatched report, generated {stamp}",
        f"Phase: {phase}",
        f"Unmatched photos: {len(unmatched)} / {len(manifest['records'])}",
        "Names below are the planned/final names. Existing GPS tags were retained.",
        "",
    ]
    for record in unmatched:
        lines.append(
            f"{record['target']} | original: {record['original']} | capture: {record['corrected']} | {record['gps_reason']}"
        )
    if not unmatched:
        lines.append("All photos matched GPS track points.")
    output.write_text("\n".join(lines) + "\n")
    logging.info("GPS unmatched report: %s (%d photos)", output, len(unmatched))
    print(f"GPS unmatched report: {output}", flush=True)
    return str(output)


def capture_instant(meta, timezone=None):
    """Interpret an existing capture time without changing its stored metadata."""
    local = captured(meta)
    offset = meta.get("ExifIFD:OffsetTimeOriginal")
    if offset:
        if not re.fullmatch(r"[+-]\d{2}:\d{2}", str(offset)):
            raise ValueError("Invalid EXIF OffsetTimeOriginal")
        return dt.datetime.fromisoformat(local.isoformat() + str(offset))
    if not timezone:
        raise ValueError("Capture time has no UTC offset; provide --timezone")
    zone = ZoneInfo(timezone)
    candidates = {
        local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        for fold in (0, 1)
        if local.replace(tzinfo=zone, fold=fold)
        .astimezone(UTC)
        .astimezone(zone)
        .replace(tzinfo=None)
        == local
    }
    if len(candidates) != 1:
        raise ValueError(
            "Capture time is ambiguous or nonexistent in --timezone; supply an EXIF UTC offset"
        )
    return next(iter(candidates)).astimezone(zone)


def completed_marker(marker):
    previous = json.loads(marker.read_text())
    state = json.loads((Path(previous["backup"]) / "state.json").read_text())
    if state["phase"] != "complete":
        raise ValueError("Previous run is unfinished; resume it first")
    return previous


def plan(args):
    folder, backup = args.folder.resolve(), args.backup.resolve()
    calibration_path = getattr(args, "calibration", None)
    timeline_path = getattr(args, "timeline", None)
    location_path = args.gpx or timeline_path

    if calibration_path and (args.reference or args.actual):
        raise ValueError(
            "--calibration cannot be combined with --reference or --actual"
        )
    correct_time = bool(calibration_path or args.reference or args.actual)
    rename = getattr(args, "rename", False)
    if (args.reference is None) != (args.actual is None):
        raise ValueError("--reference and --actual must be supplied together")
    if getattr(args, "gps_only", False) and (
        correct_time or rename or not location_path
    ):
        raise ValueError(
            "--gps-only requires --gpx or --timeline and cannot be combined with clock correction or --rename"
        )
    if not (correct_time or rename or location_path):
        raise ValueError(
            "Choose --gpx/--timeline, --reference with --actual, --calibration, or --rename"
        )
    if getattr(args, "only_missing_gps", False) and (
        correct_time or rename or not location_path
    ):
        raise ValueError(
            "--only-missing-gps requires GPS matching without time correction or renaming"
        )
    previous_run_id = None
    marker = folder / ".photo-clock-fix.json"
    if marker.exists():
        if correct_time or rename:
            raise ValueError(
                "Already processed folder: only GPS matching with unchanged names is allowed"
            )
        previous_run_id = completed_marker(marker)["run_id"]
    previous = None
    if backup.exists():
        if not args.resume:
            raise ValueError(
                "Backup directory already exists; add --resume to continue an unfinished plan"
            )
        previous = json.loads((backup / "manifest.json").read_text())
        if previous["ready"]:
            raise ValueError(
                "Plan is already complete; use apply with this recovery directory"
            )
        if previous["folder"] != str(folder):
            raise ValueError("Resume folder does not match the saved plan")
    elif args.resume:
        raise ValueError("No plan exists to resume")
    zone = ZoneInfo(args.timezone) if args.timezone else None
    actual = dt.datetime.fromisoformat(args.actual) if args.actual else None
    if correct_time:
        if zone is None:
            raise ValueError("Clock correction requires --timezone")
        if actual is not None and actual.utcoffset() is None:
            raise ValueError("--actual must include an explicit UTC offset")
        if (
            actual is not None
            and actual.utcoffset() != actual.astimezone(zone).utcoffset()
        ):
            raise ValueError("--actual UTC offset disagrees with --timezone")
    extensions = {
        "." + x.strip().lstrip(".").lower() for x in args.extensions.split(",")
    }
    files = sorted(
        p
        for p in folder.iterdir()
        if p.is_file()
        and not p.is_symlink()
        and p.suffix.lower() in extensions
        and not p.name.startswith(".")
    )
    ignored_names = []
    selection_metadata = None
    if getattr(args, "only_missing_gps", False):
        selection_metadata = exif(files, all_tags=True)
        ignored_names = [
            Path(meta["SourceFile"]).name
            for meta in selection_metadata
            if "GPS:GPSLatitude" in meta and "GPS:GPSLongitude" in meta
        ]
        files = [p for p in files if p.name not in ignored_names]
        selection_metadata = [
            meta
            for meta in selection_metadata
            if Path(meta["SourceFile"]).name not in ignored_names
        ]
    if not files:
        raise ValueError(
            "No matching photos; all selected photos may already have GPS tags"
        )
    reference = None
    if args.reference:
        reference = (
            args.reference.resolve()
            if args.reference.is_absolute()
            else folder / args.reference
        )
        if reference not in files:
            raise ValueError("Reference must be one of the selected photos")
    print(f"Reading metadata for {len(files)} photos", flush=True)
    if previous:
        old = {r["original"]: r for r in previous["records"]}
        if set(old) != {p.name for p in files}:
            raise ValueError("Photo inventory changed since the interrupted plan")
        metadata = [old[p.name]["metadata"] for p in files]
        for p in files:
            st = p.stat()
            meta = old[p.name]["metadata"]
            saved_mtime = dt.datetime.strptime(
                meta["System:FileModifyDate"], "%Y:%m:%d %H:%M:%S%z"
            ).timestamp()
            if (
                st.st_size != meta["System:FileSize"]
                or abs(st.st_mtime - saved_mtime) > 2
            ):
                raise ValueError(f"Photo changed since interrupted plan: {p}")
    else:
        metadata = (
            selection_metadata
            if selection_metadata is not None
            else exif(files, all_tags=True)
        )
    by_path = {Path(x["SourceFile"]): x for x in metadata}
    offset = (
        actual.astimezone(zone).replace(tzinfo=None) - captured(by_path[reference])
        if reference
        else dt.timedelta(0)
    )
    calibration = None
    drift = None
    if calibration_path:
        calibration = json.loads(calibration_path.read_text())
        if (
            not calibration.get("ready")
            or not calibration.get("reference")
            or not calibration.get("actual")
        ):
            raise ValueError(
                "Calibration must be a completed plan with an original clock reference"
            )
        ref_meta = next(
            r["metadata"]
            for r in calibration["records"]
            if r["original"] == calibration["reference"]
        )
        if not ref_meta.get("ExifIFD:OffsetTimeOriginal"):
            raise ValueError("Calibration reference needs an explicit EXIF UTC offset")
        actual = dt.datetime.fromisoformat(calibration["actual"])
        if actual.utcoffset() is None:
            raise ValueError("Calibration actual timestamp needs a UTC offset")
        drift = actual - capture_instant(ref_meta)
        for meta in metadata:
            if any(
                meta.get(k) != ref_meta.get(k)
                for k in (
                    "IFD0:Model",
                    "Sony:InternalSerialNumber",
                    "ExifIFD:OffsetTimeOriginal",
                )
            ):
                raise ValueError(
                    "Camera identity or UTC offset differs from calibration reference"
                )
        logging.info(
            "Reused calibration; UTC clock correction %.6f seconds",
            drift.total_seconds(),
        )
    timeline = (
        Timeline(timeline_path, getattr(args, "max_accuracy", 50))
        if timeline_path
        else None
    )
    if timeline:
        logging.info("Timeline sample filtering: %s", dict(timeline.stats))
    tracks = load_track(args.gpx) if args.gpx else []

    def natural(p):
        return [
            int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", p.name)
        ]

    use_shutter = all(
        isinstance(by_path[p].get("Sony:ShutterCount"), int) for p in files
    )
    identities = {
        (by_path[p].get("IFD0:Model"), by_path[p].get("Sony:InternalSerialNumber"))
        for p in files
    }
    if correct_time and len(identities) > 1:
        raise ValueError(
            "Mixed cameras detected; use a separate run and clock reference for each camera"
        )
    files.sort(
        key=lambda p: (
            captured(by_path[p])
            if correct_time
            else capture_instant(by_path[p], args.timezone),
            by_path[p]["Sony:ShutterCount"] if use_shutter else 0,
            natural(p),
        )
    )
    run_id = previous["run_id"] if previous else uuid.uuid4().hex
    records = []
    for i, p in enumerate(files, 1):
        corrected = (
            (captured(by_path[p]) + offset).replace(tzinfo=zone)
            if correct_time
            else capture_instant(by_path[p], args.timezone)
        )
        if drift is not None:
            corrected = (
                capture_instant(by_path[p]).astimezone(UTC) + drift
            ).astimezone(zone)
        # A single wall-clock offset cannot safely resolve a DST transition.
        if (
            correct_time
            and drift is None
            and corrected.utcoffset() != actual.utcoffset()
        ):
            raise ValueError(
                "Photos cross a timezone offset transition; split into separate runs"
            )
        name = (
            f"{args.prefix}{i:0{args.digits}d}{p.suffix.upper()}" if rename else p.name
        )
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args.prefix):
            raise ValueError(
                "Prefix must contain only letters, digits, underscores or hyphens"
            )
        target = folder / name
        if target.exists() and target not in files:
            raise ValueError(f"Destination collision: {target}")
        if strip(by_path[p], p.stat().st_size)[1] == 0:
            raise ValueError(
                f"Unsupported RAW layout: {p.name}. Expected one contiguous SubIFD strip."
            )
        records.append(
            dict(
                correct_time=correct_time,
                original=p.name,
                target=name,
                stage=f".photo-clock-{run_id}-{i}{p.suffix}",
                corrected=corrected.isoformat(timespec="microseconds"),
                gps=position(tracks, corrected, args.max_gap) if tracks else None,
                metadata=by_path[p],
            )
        )
        records[-1]["gps"], records[-1]["gps_reason"] = match_locations(
            tracks,
            timeline,
            corrected,
            args.max_gap,
            getattr(args, "timeline_max_gap", 600),
            getattr(args, "stationary_radius", 50),
            getattr(args, "stationary_max_gap", 3600),
            position,
            unmatched_reason,
        )
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "bytes").mkdir(exist_ok=True)
    if previous:
        old_records = {r["original"]: (j, r) for j, r in enumerate(previous["records"])}
        for r in records:
            j, old = old_records[r["original"]]
            if "backup" in old:
                r["backup"] = old["backup"]
                r["backup_file"] = old.get("backup_file", f"{j}.gz")
    for r in records:
        r.setdefault("backup_file", r["original"] + ".gz")
    manifest = dict(
        version=1,
        folder=str(folder),
        run_id=run_id,
        timezone=args.timezone,
        reference=reference.name if reference else None,
        correct_time=correct_time,
        rename=rename,
        previous_run_id=previous_run_id,
        ignored_names=ignored_names,
        only_missing_gps=getattr(args, "only_missing_gps", False),
        timeline_max_gap=getattr(args, "timeline_max_gap", 600),
        stationary_radius=getattr(args, "stationary_radius", 50),
        stationary_max_gap=getattr(args, "stationary_max_gap", 3600),
        actual=args.actual,
        calibration_path=str(calibration_path.resolve()) if calibration_path else None,
        calibration_sha256=hash_region(
            calibration_path, 0, calibration_path.stat().st_size
        )
        if calibration_path
        else None,
        utc_correction_seconds=drift.total_seconds() if drift is not None else None,
        timeline_path=str(timeline_path.resolve()) if timeline_path else None,
        timeline_sha256=hash_region(timeline_path, 0, timeline_path.stat().st_size)
        if timeline_path
        else None,
        timeline_stats=dict(timeline.stats) if timeline else None,
        max_accuracy=getattr(args, "max_accuracy", 50),
        offset_seconds=offset.total_seconds() if drift is None else None,
        gpx_path=str(args.gpx.resolve()) if args.gpx else None,
        exiftool_version=subprocess.check_output(
            ["exiftool", "-ver"], text=True
        ).strip(),
        gpx_sha256=hash_region(args.gpx, 0, args.gpx.stat().st_size)
        if args.gpx
        else None,
        max_gap=args.max_gap,
        tie_breaker="Sony:ShutterCount" if use_shutter else "natural filename",
        records=records,
        ready=False,
    )
    save(backup / "manifest.json", manifest)
    if drift is not None:
        print(
            f"Reused UTC clock correction: {drift.total_seconds()} seconds", flush=True
        )
    print(
        f"Clock correction: {('reused calibration' if drift is not None else str(offset.total_seconds()) + ' seconds') if correct_time else 'disabled'}; GPS matches: {sum(r['gps'] is not None for r in records)}/{len(records)}",
        flush=True,
    )
    for i, record in enumerate(records):
        if "backup" in record:
            logging.info("Reusing completed backup for %s", record["original"])
            continue
        logging.info("Backing up %d/%d %s", i + 1, len(records), record["original"])
        record["backup"] = backup_one(
            folder / record["original"],
            record["metadata"],
            backup / "bytes" / record["backup_file"],
        )
        if i % 25 == 0 or i == len(records) - 1:
            save(backup / "manifest.json", manifest)
            print(f"Backed up and hashed {i + 1}/{len(records)}", flush=True)
    manifest["ready"] = True
    save(backup / "manifest.json", manifest)
    unmatched_report(backup, manifest, "planned")
    print("Backup complete. No photos have been modified.", flush=True)


def load(backup):
    manifest = json.loads((backup / "manifest.json").read_text())
    if not manifest["ready"]:
        raise ValueError("Backup is incomplete; no changes are allowed")
    return manifest


def reconstructed(backup, index, record, source, meta, output=None):
    info = record["backup"]
    offset, length = strip(meta, source.stat().st_size)
    if length != info["length"]:
        raise ValueError(f"RAW length changed: {source}")
    for preview in info.get("previews", {}).values():
        if (
            meta.get(preview["size_tag"]) != preview["length"]
            or hash_region(source, meta[preview["start_tag"]], preview["length"])
            != preview["sha256"]
        ):
            raise ValueError(f"Embedded preview changed: {source}")
    digest, raw = hashlib.sha256(), hashlib.sha256()

    def consume(data):
        digest.update(data)
        if output:
            output.write(data)

    with (
        gzip.open(
            backup / "bytes" / record.get("backup_file", f"{index}.gz"), "rb"
        ) as saved,
        source.open("rb") as current,
    ):
        for count, stream in [
            (info["offset"], saved),
            (length, current),
            (info["size"] - info["offset"] - length, saved),
        ]:
            if stream is current:
                current.seek(offset)
            while count:
                data = stream.read(min(count, CHUNK))
                if not data:
                    raise ValueError("Truncated restoration input")
                if stream is current:
                    raw.update(data)
                consume(data)
                count -= len(data)
        if saved.read(1):
            raise ValueError("Backup has unexpected trailing data")
    if digest.hexdigest() != info["sha256"] or raw.hexdigest() != info["raw_sha256"]:
        raise ValueError(f"Byte-exact recovery/integrity check failed: {source}")
    return digest.hexdigest()


def write_args(record):
    instant = dt.datetime.fromisoformat(record["corrected"])
    stamp = instant.strftime("%Y:%m:%d %H:%M:%S")
    fraction = f"{instant.microsecond:06d}"
    zone = instant.strftime("%z")
    zone = zone[:3] + ":" + zone[3:]
    args = []
    if record.get("correct_time", True):
        for tag in [
            "ExifIFD:DateTimeOriginal",
            "ExifIFD:CreateDate",
            "IFD0:ModifyDate",
        ]:
            args.append(f"-{tag}={stamp}")
        if "IFD1:ModifyDate" in record["metadata"]:
            args.append(f"-IFD1:ModifyDate={stamp}")
        if "Sony:SonyDateTime" in record["metadata"]:
            args.append(f"-Sony:SonyDateTime={stamp}")
        for tag in ["SubSecTimeOriginal", "SubSecTimeDigitized", "SubSecTime"]:
            args.append(f"-ExifIFD:{tag}={fraction}")
        for tag in ["OffsetTimeOriginal", "OffsetTimeDigitized", "OffsetTime"]:
            args.append(f"-ExifIFD:{tag}={zone}")
    gps = record["gps"]
    if gps:
        utc = instant.astimezone(UTC)
        args += [
            f"-GPSLatitude={abs(gps['lat']):.12f}",
            f"-GPSLatitudeRef={'N' if gps['lat'] >= 0 else 'S'}",
            f"-GPSLongitude={abs(gps['lon']):.12f}",
            f"-GPSLongitudeRef={'E' if gps['lon'] >= 0 else 'W'}",
            f"-GPSDateStamp={utc:%Y:%m:%d}",
            f"-GPSTimeStamp={utc:%H:%M:%S}.{utc.microsecond:06d}",
            "-GPSMapDatum=WGS-84",
            f"-GPSProcessingMethod={gps.get('processing_method', 'GPS')}",
        ]
        if gps["altitude"] is None:
            # Do not leave stale altitude or accuracy tags from an earlier track.
            args += ["-GPSAltitude=", "-GPSAltitudeRef=", "-GPSHPositioningError="]
        if gps["altitude"] is not None:
            args += [
                f"-GPSAltitude={abs(gps['altitude']):.6f}",
                f"-GPSAltitudeRef#={0 if gps['altitude'] >= 0 else 1}",
            ]
    if record.get("set_birthtime") and record.get("correct_time", True):
        args.append(f"-FileCreateDate={stamp}.{fraction}{zone}")
    if (
        not record.get("correct_time", True)
        and record.get("set_birthtime")
        and record["backup"].get("birthtime") is not None
    ):
        birth = dt.datetime.fromtimestamp(record["backup"]["birthtime"], UTC)
        args.append(f"-FileCreateDate={birth:%Y:%m:%d %H:%M:%S}+00:00")
    return args


def check_metadata(record, meta):
    allowed = {
        "SourceFile",
        "IFD0:ModifyDate",
        "IFD1:ModifyDate",
        "ExifIFD:DateTimeOriginal",
        "ExifIFD:CreateDate",
        "Sony:SonyDateTime",
        "ExifIFD:OffsetTime",
        "ExifIFD:OffsetTimeOriginal",
        "ExifIFD:OffsetTimeDigitized",
        "ExifIFD:SubSecTime",
        "ExifIFD:SubSecTimeOriginal",
        "ExifIFD:SubSecTimeDigitized",
        "IFD0:PreviewImageStart",
        "IFD1:ThumbnailOffset",
        "SubIFD:StripOffsets",
        "SR2:SR2SubIFDLength",
        "SR2:SR2SubIFDOffset",
    }
    if not record.get("correct_time", True):
        allowed = {key for key in allowed if not ("Date" in key or "Time" in key)}
    for key in record["metadata"].keys() | meta.keys():
        if key in allowed or key.startswith(("System:", "Composite:", "ExifTool:")):
            continue
        if key.startswith("GPS:") and record["gps"]:
            continue
        if record["metadata"].get(key) != meta.get(key):
            raise ValueError(f"Unrelated metadata changed: {key}")
    instant = dt.datetime.fromisoformat(record["corrected"])
    stamp = instant.strftime("%Y:%m:%d %H:%M:%S")
    expected_zone = instant.strftime("%z")
    expected_zone = expected_zone[:3] + ":" + expected_zone[3:]
    if record.get("correct_time", True):
        for tag in [
            "ExifIFD:DateTimeOriginal",
            "ExifIFD:CreateDate",
            "IFD0:ModifyDate",
            "IFD1:ModifyDate",
            "Sony:SonyDateTime",
        ]:
            if tag in record["metadata"] or tag in [
                "ExifIFD:DateTimeOriginal",
                "ExifIFD:CreateDate",
                "IFD0:ModifyDate",
            ]:
                if meta.get(tag) != stamp:
                    raise ValueError(f"{tag} mismatch: {meta.get(tag)!r} vs {stamp!r}")
        for tag in ["OffsetTime", "OffsetTimeOriginal", "OffsetTimeDigitized"]:
            if meta.get("ExifIFD:" + tag) != expected_zone:
                raise ValueError(f"{tag} mismatch")
        for tag in ["SubSecTime", "SubSecTimeOriginal", "SubSecTimeDigitized"]:
            # ExifTool -n may present the digits as either text or a JSON integer.
            if str(meta.get("ExifIFD:" + tag)).zfill(6) != f"{instant.microsecond:06d}":
                raise ValueError(f"{tag} mismatch")
    if record["gps"]:
        if meta.get("GPS:GPSProcessingMethod") != record["gps"].get(
            "processing_method", "GPS"
        ):
            raise ValueError("GPS processing method mismatch")
        if record["gps"]["altitude"] is None and any(
            k in meta
            for k in (
                "GPS:GPSAltitude",
                "GPS:GPSAltitudeRef",
                "GPS:GPSHPositioningError",
            )
        ):
            raise ValueError("Unexpected altitude or horizontal error tag")
        for key, tag, ref in [
            ("lat", "GPSLatitude", "GPSLatitudeRef"),
            ("lon", "GPSLongitude", "GPSLongitudeRef"),
        ]:
            value = float(meta["GPS:" + tag])
            if meta["GPS:" + ref] in ["S", "W"]:
                value = -abs(value)
            if abs(value - record["gps"][key]) > 1e-7:
                raise ValueError(f"{tag} mismatch")
        altitude = record["gps"]["altitude"]
        if altitude is not None:
            read_altitude = float(meta["GPS:GPSAltitude"])
            if int(meta["GPS:GPSAltitudeRef"]) == 1:
                read_altitude = -abs(read_altitude)
            if abs(read_altitude - altitude) > 1e-4:
                raise ValueError("GPS altitude mismatch")
        utc = instant.astimezone(UTC)
        if meta.get("GPS:GPSDateStamp") != utc.strftime("%Y:%m:%d"):
            raise ValueError("GPS date mismatch")
        h, m, s = map(float, str(meta["GPS:GPSTimeStamp"]).split(":"))
        if (
            abs(
                h * 3600
                + m * 60
                + s
                - (
                    utc.hour * 3600
                    + utc.minute * 60
                    + utc.second
                    + utc.microsecond / 1e6
                )
            )
            > 1e-4
        ):
            raise ValueError("GPS time mismatch")


def apply(args):
    backup = args.backup.resolve()
    manifest = load(backup)
    folder = Path(manifest["folder"])
    marker = folder / ".photo-clock-fix.json"
    if (
        marker.exists()
        and json.loads(marker.read_text())["run_id"] != manifest["run_id"]
    ):
        old = completed_marker(marker)
        if (
            manifest.get("previous_run_id") != old["run_id"]
            or manifest.get("correct_time", True)
            or manifest.get("rename", True)
        ):
            raise ValueError("Folder belongs to a different run")
    save(marker, dict(run_id=manifest["run_id"], backup=str(backup)))
    state_path = backup / "state.json"
    state = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {"written": [], "renamed": [], "phase": "write"}
    )
    records = manifest["records"]
    if "filesystem_dates_prepared" not in manifest:
        for record in records:
            original = folder / record["original"]
            if "birthtime" not in record["backup"] and original.exists():
                record["backup"]["birthtime"] = getattr(
                    original.stat(), "st_birthtime", None
                )
            # SetFile restores the old creation date for GPS-only rewrites.
            record["set_birthtime"] = bool(
                sys.platform == "darwin"
                and shutil.which("SetFile")
                and (
                    record.get("correct_time", True)
                    or record["backup"].get("birthtime") is not None
                )
            )
        manifest["filesystem_dates_prepared"] = True
        save(backup / "manifest.json", manifest)
    extensions = {Path(r["original"]).suffix.lower() for r in records}
    allowed_names = {r[k] for r in records for k in ["original", "stage", "target"]}
    allowed_names.update(manifest.get("ignored_names", []))
    unexpected = [
        p.name
        for p in folder.iterdir()
        if p.suffix.lower() in extensions
        and not p.name.startswith(".")
        and p.name not in allowed_names
    ]
    if unexpected:
        raise ValueError(f"New photos appeared after planning: {unexpected[:5]}")
    if state["phase"] == "write":

        def process(i, r):
            logging.info(
                "Applying %d/%d %s -> %s; capture=%s; GPS=%s",
                i + 1,
                len(records),
                r["original"],
                r["target"],
                r["corrected"],
                "matched" if r["gps"] else r["gps_reason"],
            )
            if r["gps"]:
                logging.info(
                    "GPS provenance file=%s source=%s method=%s gap_seconds=%s approximate=%s",
                    r["original"],
                    r["gps"].get("source", "GPX"),
                    r["gps"]["method"],
                    r["gps"]["gap"],
                    r["gps"].get("approximate", False),
                )
            original, stage = folder / r["original"], folder / r["stage"]
            if original.exists():
                if (
                    hash_region(original, 0, original.stat().st_size)
                    != r["backup"]["sha256"]
                ):
                    raise ValueError(f"Original changed since backup: {original}")
                if stage.exists():
                    stage.unlink()  # Original verified; safely retry an interrupted write.
                edits = write_args(r)
                if not r.get("correct_time", True) and not r["gps"]:
                    shutil.copy2(original, stage)
                    if r.get("set_birthtime"):
                        result = subprocess.run(
                            ["exiftool"] + edits + [str(stage)],
                            capture_output=True,
                            text=True,
                        )
                    else:
                        result = subprocess.CompletedProcess(
                            [], 0, "Copied unchanged photo\n", ""
                        )
                else:
                    result = subprocess.run(
                        ["exiftool", "-o", str(stage)] + edits + [str(original)],
                        capture_output=True,
                        text=True,
                    )
                with (backup / "exiftool.log").open("a") as log:
                    log.write(
                        f"{dt.datetime.now(UTC).isoformat()} file={r['original']} returncode={result.returncode}\n{result.stdout}{result.stderr}\n"
                    )
                if result.returncode or "Error" in result.stderr:
                    raise ValueError(f"ExifTool failed for {original}: {result.stderr}")
            elif not stage.exists():
                raise ValueError(f"Both original and staged photo missing: {original}")
            meta = exif([stage], all_tags=True)[0]
            check_metadata(r, meta)
            reconstructed(backup, i, r, stage, meta)
            if r.get("correct_time", True):
                os.utime(
                    stage, (dt.datetime.fromisoformat(r["corrected"]).timestamp(),) * 2
                )
            else:
                os.utime(stage, ns=(r["backup"]["atime_ns"], r["backup"]["mtime_ns"]))
            os.chmod(stage, r["backup"]["mode"] & 0o777)
            # Commit the journal only after the staged file is durable.
            with stage.open("rb") as f:
                os.fsync(f.fileno())
            if original.exists():
                original.unlink()
            return i

        executor = ThreadPoolExecutor(max_workers=args.workers)
        futures = [
            executor.submit(process, i, r)
            for i, r in enumerate(records)
            if i not in state["written"]
        ]
        try:
            for future in as_completed(futures):
                i = future.result()
                state["written"].append(i)
                save(state_path, state)
                count = len(state["written"])
                if count % 10 == 0 or count == len(records):
                    print(f"Corrected and verified {count}/{len(records)}", flush=True)
        finally:
            # Stop queued work on failure; allow already-running files to finish safely.
            executor.shutdown(wait=True, cancel_futures=True)
        state["phase"] = "rename"
        save(state_path, state)
    if state["phase"] == "rename":
        for i, r in enumerate(records):
            if i in state["renamed"]:
                continue
            stage, target = folder / r["stage"], folder / r["target"]
            if stage.exists():
                if target.exists():
                    raise ValueError(f"Rename collision: {target}")
                stage.rename(target)
            elif not target.exists():
                raise ValueError(f"Rename input missing: {stage}")
            state["renamed"].append(i)
            save(state_path, state)
        state["phase"] = "complete"
        save(state_path, state)
    unmatched_report(backup, manifest, "applied")
    print("Apply complete. Run verify for the final independent readback.", flush=True)


def verify(args):
    backup = args.backup.resolve()
    manifest = load(backup)
    folder = Path(manifest["folder"])
    records = manifest["records"]
    metas = exif([folder / r["target"] for r in records], all_tags=True)
    report = dict(
        photos=len(records),
        checked=0,
        gps_matched=sum(r["gps"] is not None for r in records),
        gps_unmatched=[r["target"] for r in records if r["gps"] is None],
        offset_seconds=manifest["offset_seconds"],
        failures=[],
    )
    for i, (r, meta) in enumerate(zip(records, metas)):
        try:
            check_metadata(r, meta)
            reconstructed(backup, i, r, folder / r["target"], meta)
            if (
                abs(
                    (folder / r["target"]).stat().st_mtime
                    - (
                        dt.datetime.fromisoformat(r["corrected"]).timestamp()
                        if r.get("correct_time", True)
                        else r["backup"]["mtime_ns"] / 1e9
                    )
                )
                > 2
            ):
                raise ValueError("Filesystem modification time mismatch")
            if (
                r.get("set_birthtime")
                and abs(
                    (folder / r["target"]).stat().st_birthtime
                    - (
                        dt.datetime.fromisoformat(r["corrected"]).timestamp()
                        if r.get("correct_time", True)
                        else r["backup"]["birthtime"]
                    )
                )
                > 2
            ):
                raise ValueError("Filesystem creation time mismatch")
            report["checked"] += 1
            logging.info(
                "Verified %d/%d %s: metadata, RAW, previews and byte-exact reconstruction passed",
                i + 1,
                len(records),
                r["target"],
            )
        except Exception as e:
            logging.exception("Verification failed for %s", r["target"])
            report["failures"].append(dict(file=r["target"], error=str(e)))
        if i % 25 == 0 or i == len(records) - 1:
            print(f"Final verification {i + 1}/{len(records)}", flush=True)
            save(backup / "verification.json", report)
    if len(metas) != len(records):
        raise ValueError("Metadata record count mismatch")
    report["unmatched_report"] = unmatched_report(backup, manifest, "verified")
    save(backup / "verification.json", report)
    print(
        json.dumps({k: v for k, v in report.items() if k != "gps_unmatched"}, indent=2)
    )
    if report["failures"]:
        raise ValueError("Verification failed; see verification.json")


def restore(args):
    backup = args.backup.resolve()
    manifest = load(backup)
    folder = Path(manifest["folder"])
    destination = args.output.resolve()
    if destination == folder:
        raise ValueError(
            "Restore to a separate directory, then review before replacing photos"
        )
    destination.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(manifest["records"]):
        target = destination / r["original"]
        if target.exists():
            if hash_region(target, 0, target.stat().st_size) == r["backup"]["sha256"]:
                continue
            raise ValueError(
                f"Restore destination exists with different content: {target}"
            )
        source = None
        meta = None
        for key in ["stage", "original", "target"]:
            candidate = folder / r[key]
            if not candidate.exists():
                continue
            candidate_meta = exif([candidate])[0]
            offset, length = strip(candidate_meta, candidate.stat().st_size)
            if (
                length == r["backup"]["length"]
                and hash_region(candidate, offset, length) == r["backup"]["raw_sha256"]
            ):
                source, meta = candidate, candidate_meta
                break
        if source is None:
            raise ValueError(f"Photo missing: {r['original']}")
        temp = target.with_suffix(target.suffix + ".restoring")
        with temp.open("wb") as out:
            reconstructed(backup, i, r, source, meta, out)
            out.flush()
            os.fsync(out.fileno())
        birthtime = r["backup"].get("birthtime")
        if (
            birthtime is not None
            and sys.platform == "darwin"
            and shutil.which("SetFile")
        ):
            value = dt.datetime.fromtimestamp(birthtime, UTC).strftime(
                "%Y:%m:%d %H:%M:%S+00:00"
            )
            subprocess.run(
                ["exiftool", f"-FileCreateDate={value}", str(temp)],
                capture_output=True,
                text=True,
                check=True,
            )
        os.utime(temp, ns=(r["backup"]["atime_ns"], r["backup"]["mtime_ns"]))
        os.chmod(temp, r["backup"]["mode"] & 0o777)
        temp.rename(target)
        print(f"Restored {i + 1}/{len(manifest['records'])}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "plan",
        help="Calculate changes and create a byte-exact recovery backup; photos stay untouched",
    )
    p.add_argument("folder", type=Path)
    p.add_argument(
        "--reference", type=Path, help="Clock reference photo; requires --actual"
    )
    p.add_argument("--actual", help="ISO 8601 timestamp including UTC offset")
    p.add_argument(
        "--timezone",
        help="IANA timezone: required for clock correction, otherwise a fallback for missing EXIF offsets",
    )
    p.add_argument("--gpx", type=Path)
    p.add_argument(
        "--timeline",
        type=Path,
        help="Google device Timeline JSON export with measured rawSignals.position",
    )
    p.add_argument(
        "--max-accuracy",
        type=float,
        default=50,
        help="Maximum reported Timeline horizontal error in meters (default 50)",
    )
    p.add_argument(
        "--calibration",
        type=Path,
        help="Reuse original reference from a previous recovery manifest; requires unchanged camera clock/settings and --timezone",
    )
    p.add_argument(
        "--gps-only",
        action="store_true",
        help="Only match GPS; preserve dates and names",
    )
    p.add_argument(
        "--rename",
        action="store_true",
        help="Rename in capture order; otherwise keep existing names",
    )
    p.add_argument(
        "--max-gap",
        type=float,
        default=60,
        help="Maximum interpolation gap in seconds; no extrapolation",
    )
    p.add_argument(
        "--timeline-max-gap",
        type=float,
        default=600,
        help="Maximum Timeline interpolation or stationary corroboration sampling gap in seconds (default 600; use 60 for strict matching)",
    )
    p.add_argument(
        "--stationary-radius",
        type=float,
        default=50,
        help="Maximum observed displacement in meters for Timeline-corroborated GPX stationary gaps; 0 disables",
    )
    p.add_argument(
        "--stationary-max-gap",
        type=float,
        default=3600,
        help="Maximum corroborated stationary GPX gap in seconds (default 3600)",
    )
    p.add_argument(
        "--only-missing-gps",
        action="store_true",
        help="Only process photos without coordinates, preserving already tagged files",
    )
    p.add_argument("--extensions", default="ARW")
    p.add_argument("--prefix", default="DSC")
    p.add_argument("--digits", type=int, default=4)
    p.add_argument("--backup", type=Path, required=True)
    p.add_argument(
        "--resume",
        action="store_true",
        help="Resume an interrupted plan using its saved metadata and completed backups",
    )
    p.set_defaults(func=plan)
    for command, function in [
        ("apply", apply),
        ("verify", verify),
        ("restore", restore),
    ]:
        p = sub.add_parser(command)
        p.add_argument("--backup", type=Path, required=True)
        if command == "apply":
            p.add_argument(
                "--workers",
                type=int,
                choices=range(1, 9),
                default=2,
                help="Concurrent photo writers, default 2",
            )
        if command == "restore":
            p.add_argument("--output", type=Path, required=True)
        p.set_defaults(func=function)
    args = parser.parse_args()
    if (
        not math.isfinite(getattr(args, "max_gap", 60))
        or getattr(args, "max_gap", 60) <= 0
    ) or getattr(args, "digits", 4) < 1:
        parser.error("max-gap and digits must be positive")
    for name in (
        "timeline_max_gap",
        "stationary_max_gap",
        "max_accuracy",
        "stationary_radius",
    ):
        value = getattr(args, name, 1)
        if (
            not math.isfinite(value)
            or value < 0
            or (value == 0 and name != "stationary_radius")
        ):
            parser.error(
                f"{name.replace('_', '-')} must be finite and positive (stationary-radius may be zero)"
            )
    if not shutil.which("exiftool"):
        parser.error("Install ExifTool first: https://exiftool.org/")
    args.backup = args.backup.resolve()
    args.backup.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    log_path = args.backup.parent / f"{args.backup.name}-{stamp}-{args.command}.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    logging.info(
        "Command %s; Python %s; platform %s; arguments %s",
        args.command,
        sys.version,
        platform.platform(),
        vars(args),
    )
    print(f"Operation log: {log_path}", flush=True)
    lock_path = args.backup.parent / f".{args.backup.name}.lock"
    try:
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another command is using this recovery directory")
            args.func(args)
        logging.info("Command completed successfully")
    except Exception as e:
        logging.exception("Command failed")
        print(f"Error: {e}. Details: {log_path}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        logging.warning("Interrupted by user; use the saved journal to resume apply")
        print(f"Interrupted. Details: {log_path}", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
