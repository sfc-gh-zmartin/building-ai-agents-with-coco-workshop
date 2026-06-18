#!/usr/bin/env python3
"""
download_gh_archive_to_s3.py
-----------------------------
Downloads 30 days of GH Archive hourly files and uploads to S3.
Strips the heavy 'payload' field to reduce size by ~50%.

Usage:
    pip install requests boto3
    python3 download_gh_archive_to_s3.py

Configuration:
    Set S3_BUCKET and S3_PREFIX below.
    AWS credentials via env vars or ~/.aws/credentials (standard boto3 auth).
"""

import os
import gzip
import json
import io
import logging
import concurrent.futures
from datetime import datetime, timedelta, timezone

import requests
import boto3

# ──────────────────────────────────────────────
# CONFIG — edit these
# ──────────────────────────────────────────────
S3_BUCKET  = "YOUR_BUCKET_NAME"           # e.g. "gh-archive-workshop"
S3_PREFIX  = "gh-archive/"                # folder inside the bucket
DAYS_BACK  = 30                           # how many days to load
WORKERS    = 4                            # parallel downloads
GH_URL     = "https://data.gharchive.org/{year}-{month:02d}-{day:02d}-{hour}.json.gz"

# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

s3 = boto3.client("s3")


def get_hourly_slots(days_back: int) -> list[dict]:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    slots = []
    for delta in range(days_back * 24, 0, -1):
        dt = now - timedelta(hours=delta)
        slots.append({
            "url":   GH_URL.format(year=dt.year, month=dt.month, day=dt.day, hour=dt.hour),
            "label": dt.strftime("%Y-%m-%d-%H"),
            "key":   f"{S3_PREFIX}{dt.strftime('%Y-%m-%d-%H')}.json.gz",
        })
    return slots


def process_and_upload(slot: dict) -> bool:
    """Download, strip payload, upload to S3. Returns True on success."""
    try:
        resp = requests.get(slot["url"], timeout=60)
        if resp.status_code == 404:
            log.warning("Not found (skipping): %s", slot["label"])
            return False
        resp.raise_for_status()

        # Decompress, strip payload, recompress in memory
        buf = io.BytesIO()
        with gzip.open(io.BytesIO(resp.content)) as fin, \
             gzip.open(buf, "wt", encoding="utf-8") as fout:
            count = 0
            for line in fin:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Strip payload — not needed, saves ~50% size
                fout.write(json.dumps({
                    "id":         ev.get("id"),
                    "type":       ev.get("type"),
                    "actor":      ev.get("actor"),
                    "repo":       ev.get("repo"),
                    "org":        ev.get("org"),
                    "public":     ev.get("public"),
                    "created_at": ev.get("created_at"),
                }) + "\n")
                count += 1

        # Upload to S3
        buf.seek(0)
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=slot["key"],
            Body=buf.getvalue(),
            ContentType="application/gzip",
        )
        log.info("Uploaded %-22s  (%d events)", slot["label"], count)
        return True

    except Exception as exc:
        log.error("Failed %s: %s", slot["label"], exc)
        return False


def main():
    slots = get_hourly_slots(DAYS_BACK)
    log.info("Uploading %d hourly files (%d days) → s3://%s/%s",
             len(slots), DAYS_BACK, S3_BUCKET, S3_PREFIX)

    success = fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for result in pool.map(process_and_upload, slots):
            if result:
                success += 1
            else:
                fail += 1

    log.info("Done — %d uploaded, %d skipped/failed", success, fail)
    log.info("S3 URI: s3://%s/%s", S3_BUCKET, S3_PREFIX)


if __name__ == "__main__":
    main()
