#!/usr/bin/env python3
"""Compatibility entry point. New commands use photo_time_gps.py."""

from photo_time_gps import main

if __name__ == "__main__":
    raise SystemExit(main())
