"""Conservative matching of measured positions from Google device Timeline exports."""

import bisect
from collections import Counter
import datetime as dt
import json
import math
import re

from timestamps import parse_iso_datetime


def timestamp(value):
    instant = parse_iso_datetime(value)
    if instant.utcoffset() is None:
        raise ValueError("Timeline timestamps must include a UTC offset")
    return instant.timestamp()


class Timeline:
    """Use rawSignals.position; inferred visits and paths are not measurements."""

    def __init__(self, path, max_accuracy=50):
        if not math.isfinite(max_accuracy) or max_accuracy <= 0:
            raise ValueError("Maximum Timeline accuracy must be positive")
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("rawSignals"), list):
            raise ValueError(
                "Expected Google device export with rawSignals.position; other Timeline formats are unsupported"
            )
        self.stats = Counter()
        candidates = {}
        for signal in data["rawSignals"]:
            if "position" not in signal:
                continue
            p = signal["position"]
            self.stats["positions"] += 1
            try:
                t = timestamp(p["timestamp"])
                coordinate = re.fullmatch(
                    r"\s*([+-]?\d+(?:\.\d+)?)°?\s*,\s*([+-]?\d+(?:\.\d+)?)°?\s*",
                    p["LatLng"],
                )
                if not coordinate:
                    raise ValueError("Invalid coordinates")
                lat, lon = map(float, coordinate.groups())
                accuracy = float(p.get("accuracyMeters", math.inf))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Invalid coordinates")
                if not math.isfinite(accuracy) or not 0 < accuracy <= max_accuracy:
                    self.stats["inaccurate_or_missing_accuracy"] += 1
                    continue
                # Altitude datum / vertical accuracy are unspecified, so omit altitude.
                candidates.setdefault(t, []).append(
                    (lat, lon, accuracy, p.get("source", "UNKNOWN"))
                )
            except (ValueError, TypeError, KeyError):
                self.stats["malformed"] += 1
        self.samples = {}
        for t, values in candidates.items():
            if len({v[:2] for v in values}) != 1:
                self.stats["conflicting_timestamps"] += 1
                continue
            self.samples[t] = min(values, key=lambda v: v[2])
        self.times = sorted(self.samples)
        self.tracks = (
            [[(t, *self.samples[t][:2], None) for t in self.times]]
            if self.times
            else []
        )
        self.stats["accepted"] = len(self.times)
        if not self.times:
            raise ValueError(
                "Timeline contains no usable measured positions within the accuracy limit"
            )
        self.max_accuracy = max_accuracy

    def match(self, instant, max_gap, interpolate, reason):
        result = interpolate(self.tracks, instant, max_gap)
        if result is None:
            return (
                None,
                reason(self.tracks, instant, max_gap)
                + f" Timeline uses measured positions with reported accuracy at most {self.max_accuracy:g} m; inferred visits/paths are excluded.",
            )
        i = bisect.bisect_left(self.times, instant.timestamp())
        times = (
            [self.times[i]]
            if result["method"] == "exact"
            else self.times[i - 1 : i + 1]
        )
        result["source"] = "Google Timeline rawSignals.position"
        result["sources"] = sorted({self.samples[t][3] for t in times})
        result["sample_times_utc"] = [
            dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat() for t in times
        ]
        result["reported_accuracy_meters"] = max(self.samples[t][2] for t in times)
        # EXIF has no suitable standard code for fused Wi-Fi/cell/phone positions.
        result["processing_method"] = (
            "GPS" if result["sources"] == ["GPS"] else "MANUAL"
        )
        return result, None
