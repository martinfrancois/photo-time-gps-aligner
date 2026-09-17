#!/usr/bin/env python3
"""Compare annotated Geo Tracker stops with GPX gaps without editing photos."""

import argparse
import bisect
import datetime as dt
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from matching import distance, match
from photo_time_gps import load_track, position, unmatched_reason


def audit(path, max_gap=60):
    tracks = load_track(path)
    root = ET.parse(path).getroot()
    gaps = []
    for segment_index, points in enumerate(tracks):
        for index, (left, right) in enumerate(zip(points, points[1:])):
            if right[0] - left[0] > max_gap:
                gaps.append(
                    {
                        "segment": segment_index,
                        "index": index,
                        "start": dt.datetime.fromtimestamp(
                            left[0], dt.timezone.utc
                        ).isoformat(),
                        "end": dt.datetime.fromtimestamp(
                            right[0], dt.timezone.utc
                        ).isoformat(),
                        "seconds": right[0] - left[0],
                        "endpoint_distance_meters": distance(left, right),
                    }
                )
    stops = []
    for waypoint in root.findall("{*}wpt"):
        label = waypoint.findtext("{*}name", "")
        duration = re.fullmatch(
            r"Stop for\s+~?(\d+(?:\.\d+)?)\s+min", label, re.IGNORECASE
        )
        if not duration:
            continue
        value = waypoint.findtext("{*}time")
        if not value:
            raise ValueError(f"Stop waypoint has no timestamp: {label}")
        instant = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.utcoffset() is None:
            raise ValueError("Stop waypoint timestamps require a UTC offset")
        t = instant.timestamp()
        adjacent = []
        for si, points in enumerate(tracks):
            times = [p[0] for p in points]
            i = bisect.bisect_left(times, t)
            # A marker can be at the start, inside, or at the end of a pause.
            # Report all adjacent candidates; never invent exact stop boundaries.
            indices = [i - 1, i] if i < len(times) and times[i] == t else [i - 1]
            for index in indices:
                if not 0 <= index < len(points) - 1:
                    continue
                left, right = points[index : index + 2]
                if not left[0] <= t <= right[0] or right[0] - left[0] <= max_gap:
                    continue
                row = next(
                    g.copy() for g in gaps if g["segment"] == si and g["index"] == index
                )
                midpoint = dt.datetime.fromtimestamp(
                    (left[0] + right[0]) / 2, dt.timezone.utc
                )
                result, why = match(
                    tracks,
                    None,
                    midpoint,
                    max_gap,
                    600,
                    50,
                    3600,
                    position,
                    unmatched_reason,
                )
                row["gpx_only_midpoint_matched"] = result is not None
                row["gpx_only_reason"] = why
                adjacent.append(row)
        stops.append(
            {
                "label": label,
                "marker_time": instant.isoformat(),
                "approximate_duration_seconds": float(duration[1]) * 60,
                "adjacent_gaps": adjacent,
                "combined_adjacent_gap_seconds": sum(g["seconds"] for g in adjacent),
            }
        )
    if not stops:
        raise ValueError('No timestamped waypoints named "Stop for ~N min" found')
    referenced = {(g["segment"], g["index"]) for s in stops for g in s["adjacent_gaps"]}
    return {
        "stop_count": len(stops),
        "track_point_count": sum(map(len, tracks)),
        "max_gap_seconds": max_gap,
        "stops": stops,
        "long_gaps": gaps,
        "long_gaps_without_stop_marker": [
            g for g in gaps if (g["segment"], g["index"]) not in referenced
        ],
        "interpretation": "Stop labels and durations are user annotations, not precise start/end boundaries. Unmarked gaps are unconfirmed, not proven movement or stops. No coordinates or photo files were changed.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gpx", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.gpx)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"Compared {result['stop_count']} annotated stops with {len(result['long_gaps'])} long gaps. Report: {args.output}"
    )


if __name__ == "__main__":
    main()
