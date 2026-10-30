"""ISO timestamps shared by calibration and location imports."""

import datetime as dt
import re


def parse_iso_datetime(value):
    # Python 3.10 accepts only three or six fractional digits. Match newer
    # Python's microsecond truncation without converting through a float.
    value = re.sub(
        r"(?<=[0-9]{2}:[0-9]{2}:[0-9]{2})[.,]([0-9]+)",
        lambda match: "." + match[1][:6].ljust(6, "0"),
        value,
    )
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return dt.datetime.fromisoformat(value)
