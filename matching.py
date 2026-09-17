"""Match GPX or Timeline, including optional recorder-specific stop estimates."""

import bisect
import math
import re
import datetime as dt


def distance(p, q):
    lat1, lon1, lat2, lon2 = map(math.radians, [p[1], p[2], q[1], q[2]])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 12742000 * math.asin(min(1, math.sqrt(h)))


def stationary_estimate(tracks, instant, radius, max_gap, interpolate):
    policy = getattr(tracks, "gap_policy", "auto")
    creator = getattr(tracks, "creator", "")
    enabled = policy == "stationary" or (
        policy == "auto" and re.search(r"\bgeo\s*tracker\b", creator, re.IGNORECASE)
    )
    if not enabled or radius <= 0:
        return None
    t = instant.timestamp()
    for segment in tracks:
        i = bisect.bisect_left([p[0] for p in segment], t)
        if not 0 < i < len(segment):
            continue
        left, right = segment[i - 1 : i + 1]
        gap = right[0] - left[0]
        separation = distance(left, right)
        if gap > max_gap or separation > 2 * radius:
            continue
        # A fixed great-circle midpoint avoids inventing motion during a stop.
        center_time = dt.datetime.fromtimestamp(
            (left[0] + right[0]) / 2, dt.timezone.utc
        )
        result = interpolate([[left, right]], center_time, max_gap)
        result.update(
            method="stationary_estimate",
            source="GPX stop estimate",
            processing_method="MANUAL",
            altitude=None,
            approximate=True,
            recorder=creator,
            gpx_gap_policy=policy,
            endpoint_distance_meters=separation,
            observed_radius_meters=separation / 2,
            stationary_radius_meters=radius,
            sample_times_utc=[
                dt.datetime.fromtimestamp(p[0], dt.timezone.utc).isoformat()
                for p in [left, right]
            ],
            assumption="Continuous movement-filtered recording; nearby endpoints suggest a stop but cannot rule out an unrecorded excursion. Observed radius is not a GPS accuracy bound.",
        )
        return result
    return None


def match(
    tracks,
    timeline,
    instant,
    max_gap,
    timeline_gap,
    stationary_radius,
    stationary_gap,
    interpolate,
    reason,
):
    sources = getattr(tracks, "sources", [tracks])
    for source in sources:
        result = interpolate(source, instant, max_gap)
        if result:
            result["source"] = "GPX"
            if hasattr(source, "path"):
                result["gpx_path"] = source.path
            return result, None
    if timeline:
        result, why = timeline.match(instant, timeline_gap, interpolate, reason)
        if result:
            result["approximate"] = result["gap"] > max_gap
            return result, None
    else:
        why = reason(tracks, instant, max_gap)
    result = None
    for source in sources:
        result = stationary_estimate(
            source, instant, stationary_radius, stationary_gap, interpolate
        )
        if result:
            if hasattr(source, "path"):
                result["gpx_path"] = source.path
            break
    if result:
        if timeline:
            start, end = [
                dt.datetime.fromisoformat(t).timestamp()
                for t in result["sample_times_utc"]
            ]
            evidence = timeline.tracks[0][
                bisect.bisect_left(timeline.times, start) : bisect.bisect_right(
                    timeline.times, end
                )
            ]
            center = (instant.timestamp(), result["lat"], result["lon"], None)
            if any(distance(center, p) > stationary_radius for p in evidence):
                return (
                    None,
                    why + " Timeline observations contradict the GPX stop estimate.",
                )
            result["optional_timeline_observations"] = len(evidence)
        return result, None
    return (
        None,
        why
        + " GPX stop estimate disabled, outside a segment, or beyond its spatial/time limits.",
    )
