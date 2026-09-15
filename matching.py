"""Match measured tracks, with explicit evidence for stationary gaps."""

import bisect
import math


def distance(p, q):
    lat1, lon1, lat2, lon2 = map(math.radians, [p[1], p[2], q[1], q[2]])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 12742000 * math.asin(min(1, math.sqrt(h)))


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
    if tracks:
        result = interpolate(tracks, instant, max_gap)
        if result:
            result["source"] = "GPX"
            return result, None
    if timeline:
        result, why = timeline.match(instant, timeline_gap, interpolate, reason)
        if result:
            result["approximate"] = result["gap"] > max_gap
            return result, None
    else:
        why = reason(tracks, instant, max_gap)
    if not tracks or not timeline or stationary_radius <= 0:
        return None, why
    t = instant.timestamp()
    for segment in tracks:
        i = bisect.bisect_left([p[0] for p in segment], t)
        if not 0 < i < len(segment):
            continue
        left, right = segment[i - 1 : i + 1]
        gap = right[0] - left[0]
        if gap > stationary_gap or distance(left, right) > stationary_radius:
            continue
        evidence = timeline.tracks[0][
            bisect.bisect_right(timeline.times, left[0]) : bisect.bisect_left(
                timeline.times, right[0]
            )
        ]
        if len(evidence) < 3:
            continue
        excursion = max(distance(left, p) for p in evidence)
        observations = [left, *evidence, right]
        unseen = max(b[0] - a[0] for a, b in zip(observations, observations[1:]))
        if excursion > stationary_radius or unseen > timeline_gap:
            continue
        result = interpolate([[left, right]], instant, stationary_gap)
        result.update(
            method="stationary_estimate",
            source="GPX corroborated by Google Timeline",
            processing_method="MANUAL",
            altitude=None,
            approximate=True,
            corroborating_samples=len(evidence),
            max_excursion_meters=excursion,
            endpoint_distance_meters=distance(left, right),
            largest_unsampled_gap_seconds=unseen,
            stationary_radius_meters=stationary_radius,
            assumption="Phone stayed near the observed stationary positions between samples",
        )
        return result, None
    return (
        None,
        why
        + " Stationary fallback lacked close GPX endpoints and sufficient nearby Timeline observations.",
    )
