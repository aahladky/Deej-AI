"""
import_spotify.py — Seed plays.db with historical Spotify streaming data.

Reads all JSON files from the Spotify Extended Streaming History export and
inserts them into plays.db using the same schema as station_server.

Track matching strategy:
    - Spotify records have no local file path, so track_path is set to
      "spotify:<uri>" as a placeholder.  The scorer keys on artist name,
      so these rows still feed the behavioral stats correctly.
    - Only music tracks are imported (ms_played > 0, track name present).

Context reconstruction:
    - Spotify timestamps are UTC ISO-8601.  Convert to US/Eastern and apply
      the same weekday + 16:30-21:00 rule as station_server.

Completion mapping:
    reason_end = 'trackdone'          -> completed = 1
    reason_end = 'fwdbtn' / 'logout'  -> completed = 0
    anything else                     -> completed = 0  (conservative)

Run once (idempotent via unique constraint on spotify_uri + started_at):
    python import_spotify.py
"""

import json
import os
import sqlite3
import glob
from datetime import datetime, timezone, timedelta

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

HISTORY_DIR = r'C:\Users\aahla\Downloads\my_spotify_data\Spotify Extended Streaming History'
DB_PATH     = r'C:\Dev\DeeJAI\radio\plays.db'

# ET offset (standard: UTC-5; DST: UTC-4). We use a simple rule:
# Mar second-Sun through Nov first-Sun = EDT (UTC-4), else EST (UTC-5).
# Python's zoneinfo handles this cleanly if available; otherwise we fall back.
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo('America/New_York')
except ImportError:
    ET = None   # fallback below

GYM_DAYS    = {0, 1, 2, 3, 4}
GYM_START_H = 16.5
GYM_END_H   = 21.0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _et_offset(utc_dt: datetime) -> timedelta:
    """Approximate EST/EDT offset without zoneinfo."""
    year = utc_dt.year
    # DST starts 2nd Sunday in March, ends 1st Sunday in November
    dst_start = datetime(year, 3,  8, 2, tzinfo=timezone.utc)
    dst_start += timedelta(days=(6 - dst_start.weekday()) % 7)
    dst_end   = datetime(year, 11, 1, 2, tzinfo=timezone.utc)
    dst_end   += timedelta(days=(6 - dst_end.weekday()) % 7)
    if dst_start <= utc_dt < dst_end:
        return timedelta(hours=-4)   # EDT
    return timedelta(hours=-5)       # EST


def utc_to_et(ts_str: str) -> datetime:
    """Parse Spotify UTC timestamp and return ET-localised datetime."""
    # Format: '2024-03-15T18:42:11Z' or '2024-03-15T18:42:11'
    ts_str = ts_str.rstrip('Z')
    utc_dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
    if ET is not None:
        return utc_dt.astimezone(ET)
    return utc_dt + _et_offset(utc_dt)


def get_context(et_dt: datetime) -> str:
    hour = et_dt.hour + et_dt.minute / 60.0
    if et_dt.weekday() in GYM_DAYS and GYM_START_H <= hour <= GYM_END_H:
        return 'gym'
    return 'home'


def map_completed(reason_end: str) -> int:
    return 1 if reason_end == 'trackdone' else 0


# --------------------------------------------------------------------------- #
# DB setup
# --------------------------------------------------------------------------- #

def ensure_db(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute('''
        CREATE TABLE IF NOT EXISTS plays (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            track_path TEXT,
            artist     TEXT,
            title      TEXT,
            album      TEXT,
            started_at REAL,
            ms_played  INTEGER,
            completed  INTEGER,
            context    TEXT
        )
    ''')
    # Unique index prevents duplicate imports
    con.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_plays_spotify_dedup
        ON plays (track_path, started_at)
    ''')
    con.commit()
    return con


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #

def import_history(history_dir: str, db_path: str):
    con = ensure_db(db_path)

    files = sorted(glob.glob(os.path.join(history_dir, '*.json')))
    if not files:
        print(f"No JSON files found in {history_dir}")
        return

    print(f"Found {len(files)} file(s). Importing...\n")

    total_rows    = 0
    inserted      = 0
    skipped_dupe  = 0
    skipped_junk  = 0

    for fpath in files:
        fname = os.path.basename(fpath)
        with open(fpath, encoding='utf-8') as f:
            records = json.load(f)

        file_inserted = 0
        for rec in records:
            total_rows += 1

            # Skip non-music or empty rows
            ts        = rec.get('ts')
            artist    = rec.get('master_metadata_album_artist_name') or ''
            title     = rec.get('master_metadata_track_name') or ''
            ms_played = rec.get('ms_played', 0)
            uri       = rec.get('spotify_track_uri') or ''

            if not ts or not title or ms_played <= 0:
                skipped_junk += 1
                continue

            et_dt      = utc_to_et(ts)
            started_at = et_dt.timestamp()
            context    = get_context(et_dt)
            completed  = map_completed(rec.get('reason_end') or '')
            album      = rec.get('master_metadata_album_album_name') or ''
            track_path = f"spotify:{uri}" if uri else f"spotify:unknown:{title}"

            try:
                con.execute(
                    'INSERT INTO plays VALUES (NULL,?,?,?,?,?,?,?,?)',
                    (track_path, artist, title, album,
                     started_at, ms_played, completed, context)
                )
                file_inserted += 1
                inserted      += 1
            except sqlite3.IntegrityError:
                skipped_dupe += 1

        con.commit()
        print(f"  {fname}: inserted {file_inserted} / {len(records)} records")

    con.close()
    print(f"\nDone. Total rows: {total_rows}")
    print(f"  Inserted  : {inserted}")
    print(f"  Skipped   : {skipped_junk} (no title/ms) + {skipped_dupe} duplicates")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

if __name__ == '__main__':
    import sys
    hdir = sys.argv[1] if len(sys.argv) > 1 else HISTORY_DIR
    db   = sys.argv[2] if len(sys.argv) > 2 else DB_PATH
    print(f"History dir : {hdir}")
    print(f"DB          : {db}\n")
    import_history(hdir, db)
