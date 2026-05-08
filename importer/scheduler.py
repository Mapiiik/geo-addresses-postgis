#!/usr/bin/env python3
"""
Importer scheduler — runs all configured imports on a monthly cron-like schedule.

Designed to be the default Docker entrypoint: the container stays up forever,
sleeps most of the time, and wakes up once a month to refresh the data.

Configuration (env vars):
  SCHEDULE_DAY     day of month (1-28). Default: 5
  SCHEDULE_HOUR    hour 0-23.            Default: 3
  SCHEDULE_MINUTE  minute 0-59.          Default: 0
  RUN_ON_START     "1" → run imports immediately on container start (useful
                   for first-time bootstrap). Default: "0".

Default = the 5th of every month at 03:00 (local container time). RUIAN dumps
are typically published in the first few days of the new month, so the 5th
gives CUZK a comfortable margin.
"""
import logging
import os
import time
from datetime import datetime

from importer import import_cz_csv, import_hr_wfs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("scheduler")


SCHEDULE_DAY    = int(os.getenv("SCHEDULE_DAY",    "5"))
SCHEDULE_HOUR   = int(os.getenv("SCHEDULE_HOUR",   "3"))
SCHEDULE_MINUTE = int(os.getenv("SCHEDULE_MINUTE", "0"))
RUN_ON_START    = os.getenv("RUN_ON_START", "0") == "1"

# (label, callable) — order = run order each month
JOBS = [
    ("CZ RUIAN (CSV)", import_cz_csv.main),
    ("HR DGU (WFS)",   import_hr_wfs.main),
]


def next_run_at(now):
    """Return the next datetime >= now that matches the configured schedule."""
    candidate = now.replace(
        day=SCHEDULE_DAY,
        hour=SCHEDULE_HOUR,
        minute=SCHEDULE_MINUTE,
        second=0,
        microsecond=0,
    )
    if candidate > now:
        return candidate
    # Roll forward to the same day in the next month
    if candidate.month == 12:
        return candidate.replace(year=candidate.year + 1, month=1)
    return candidate.replace(month=candidate.month + 1)


def run_all():
    """Run every configured job. One job's failure does not stop the others."""
    for label, func in JOBS:
        log.info("=== Running job: %s ===", label)
        try:
            func()
            log.info("=== Job finished: %s ===", label)
        except Exception:
            # Log and keep going — the daemon must survive single-job failures
            log.exception("Job %r failed; continuing with remaining jobs.", label)


def main():
    log.info(
        "Scheduler started. Schedule: day=%d at %02d:%02d, RUN_ON_START=%s",
        SCHEDULE_DAY, SCHEDULE_HOUR, SCHEDULE_MINUTE, RUN_ON_START,
    )

    if RUN_ON_START:
        log.info("RUN_ON_START=1 → running imports immediately.")
        run_all()

    while True:
        target = next_run_at(datetime.now())
        wait_s = (target - datetime.now()).total_seconds()
        log.info("Next run at %s (sleeping %.0f s)", target.isoformat(timespec="seconds"), wait_s)
        # Guard against negative sleep if the clock drifted; min 1 s.
        time.sleep(max(wait_s, 1.0))
        run_all()


if __name__ == "__main__":
    main()
