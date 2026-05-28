# Music Import Pipeline — Technical Plan

**Project:** Local radio station backend (station_server.py)  
**Status:** Pre-implementation reference document  
**Date:** 2026-05-19  

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Component Breakdown](#3-component-breakdown)
4. [File & Folder Layout](#4-file--folder-layout)
5. [Beets Configuration](#5-beets-configuration)
6. [Manual Review Workflow](#6-manual-review-workflow)
7. [Lossless Handling Rules](#7-lossless-handling-rules)
8. [Incremental Embedding & Cache Update](#8-incremental-embedding--cache-update)
9. [Server Reload](#9-server-reload)
10. [Error Handling & Rollback](#10-error-handling--rollback)
11. [Genre Management](#11-genre-management)
12. [Admin Dashboard Authentication (WebAuthn / Passkeys)](#12-admin-dashboard-authentication-webauthn--passkeys)
13. [Phased Build Order](#13-phased-build-order)

---

## 1. System Overview

The existing system is a Flask-based radio station server (`station_server.py`) that serves tracks from `E:\Media\Music`. Embeddings are precomputed by `MP3ToVec.py` into `mp3tovecs.p` (loaded at startup). Metadata is cached in `radio/track_meta.json`. Play history lives in `plays.db` (SQLite), with Spotify history bridged via `import_spotify.py`.

The import pipeline adds a supervised intake process on top of this existing system:

- A **watchdog service** monitors a staging inbox (`E:\Media\MusicInbox`).
- New files are processed by **beets**, which queries MusicBrainz and applies tags and a canonical folder structure.
- High-confidence matches go directly into `E:\Media\Music` after embedding and metadata updates.
- Low-confidence matches go to `E:\Media\MusicPendingReview\` for human review via the admin dashboard.
- After any confirmed import, embeddings are updated **incrementally** (not a full rescan) and the server is signaled to reload.
- New embeddings are integrated with the existing recommendation system (MP3ToVec / `mp3tovecs.p`) so imported tracks become immediately eligible for playback and similarity-based recommendations.

**Inbox archival policy:** After a fully successful import (beets tagging + embedding generation + metadata cache update), the original inbox file is moved to `E:\Media\_MusicInboxArchive\` rather than deleted outright. Archive files are automatically purged after 90 days. This provides a safety net for recovering from bad imports without requiring the user to re-source the file.

**Reject behavior:** Files rejected from the dashboard are deleted from `MusicPendingReview\`. A structured deletion history log records the file path, original beets suggestion, rejection timestamp, and optional reason. The inbox original, already moved to the archive at routing time, is left in the archive (and subject to normal 90-day purge).

---

## 2. Architecture Diagram

```
E:\Media\MusicInbox\          E:\Media\MusicPendingReview\
       │                               ▲
       │  (watchdog detects new file)  │
       ▼                               │ low confidence / unmatched album
┌─────────────────────────────────────────────────────────┐
│                   import_watcher.py                     │
│   1. copy to working area (_MusicStaging\)              │
│   2. run beets import (--timid for confidence check)    │
│   3. route by confidence score                          │
│   4. on success: move inbox original to _MusicInboxArchive\ │
└─────────────────────────────────────────────────────────┘
       │ high confidence                │ failure / error
       ▼                               ▼
┌──────────────┐               ┌──────────────────────────┐
│  beets moves │               │  file left in inbox      │
│  file to:    │               │  moved to _MusicFailed\  │
│  E:\Media\   │               │  alert logged            │
│  Music\...   │               └──────────────────────────┘
└──────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│                post_import.py                           │
│   1. run MP3ToVec.py on new file path(s) only           │
│   2. merge new embeddings into mp3tovecs.p              │
│   3. update radio/track_meta.json (incremental)         │
│   4. apply genre normalization (controlled list)        │
│   5. POST /reload → station_server.py                   │
└─────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│              station_server.py (/reload)                │
│   reloads mp3tovecs.p + track_meta.json in memory       │
└─────────────────────────────────────────────────────────┘

Parallel path (pending review):
┌─────────────────────────────────────────────────────────┐
│         Admin Dashboard (/admin/review)  [passkey auth] │
│   lists pending tracks with beets suggestions           │
│   actions: Confirm / Edit tags / Reject                 │
│   album unmatched: Import individually OR override      │
└─────────────────────────────────────────────────────────┘
       │ confirm/edit                   │ reject
       ▼                               ▼
  post_import.py              file deleted from PendingReview\
  (same as above)             entry written to rejection_log.jsonl

Archive cleanup (background / scheduled):
┌─────────────────────────────────────────────────────────┐
│   _MusicInboxArchive\ → purge files older than 90 days  │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Component Breakdown

### 3.1 Watcher Service (`import_watcher.py`)

**Purpose:** Continuously monitors `E:\Media\MusicInbox` for new audio files and orchestrates the import pipeline.

**Technology:** Python `watchdog` library (`Observer` + `FileSystemEventHandler`).

**Behavior:**
- Watches for `on_created` and `on_moved` events on recognized audio extensions: `.mp3`, `.flac`, `.m4a`, `.aac`, `.ogg`, `.wav`, `.aiff`.
- Applies a short stabilization delay (3 seconds by default) after detection to ensure the file has finished writing (especially for large FLACs transferred over a network).
- For **individual files**: copies to `E:\Media\_MusicStaging\<uuid>\` and processes immediately.
- For **folder drops**: applies a quiescence timer (default: 10 seconds with no new arrivals in the folder) before treating the folder as a complete album unit and handing it to beets as a directory import.
- After a fully successful import, moves the inbox original to `E:\Media\_MusicInboxArchive\<YYYY-MM\original_filename>` (month-namespaced to keep it navigable). Does NOT delete it.
- On failure: leaves inbox file intact, writes to error log, moves staged copy to `E:\Media\_MusicFailed\`.

**Running as a Windows Service:** The watcher runs as a Windows Service using `pywin32` (`win32serviceutil`). This provides automatic start-on-boot (no user login required), crash restart via the Service Control Manager, and visibility in the Windows Services panel. See §13 (Phase 5) for setup steps.

**Concurrency:** Serializes imports (one at a time) via a `threading.Lock` to prevent race conditions on `mp3tovecs.p` and `track_meta.json`. Folder imports are treated as a single atomic unit within this lock.

---

### 3.2 Beets Import Subprocess

**Purpose:** Tag files using MusicBrainz and move them to the canonical library path under `E:\Media\Music`.

**Invocation:** The watcher calls beets via `subprocess.run(["beet", "import", "--timid", path])`, capturing stdout to parse the confidence score before deciding whether to commit the import.

**Confidence threshold:** Default is **90%**, configurable via the admin dashboard (see §6) or directly by editing `C:\Dev\import_pipeline\config.json`. The threshold is read at import time, not cached at startup, so dashboard changes take effect immediately on the next import.

**Confidence routing:**

| Confidence | Action |
|---|---|
| ≥ configured threshold (default 90%) | Auto-import to `E:\Media\Music\` |
| < threshold | Move staged copy to `E:\Media\MusicPendingReview\`, write `.json` sidecar |
| Beets errors / no MusicBrainz match | Move to `E:\Media\_MusicFailed\`, log error |
| Folder drop / album unmatched in MusicBrainz | Route to `E:\Media\MusicPendingReview\` as `album_review` type (see §3.5) |

**Note on "auto" mode:** For the auto-import path, beets runs with `--write` to apply tags. For the pending path, the file is staged without committing beets tags; suggestions are stored in the sidecar for the dashboard to apply.

**Duplicate detection:** Handled by the `beets-duplicate` plugin (see §5). If a duplicate is detected, routing defers to the lossless preference rules in §7.

---

### 3.3 Staging & Working Area Logic

The staging area (`E:\Media\_MusicStaging\`) is an intermediate buffer that prevents partial writes to the main library. Lifecycle:

1. `import_watcher.py` copies inbox file(s) → `_MusicStaging\<uuid>\`
2. Beets runs against `_MusicStaging\<uuid>\`
3. If high confidence: beets moves file(s) to `E:\Media\Music\<canonical path>`
4. If low confidence: watcher moves file(s) to `E:\Media\MusicPendingReview\` + writes sidecar `.json`
5. If failure: watcher moves to `E:\Media\_MusicFailed\`
6. Inbox original is moved to `E:\Media\_MusicInboxArchive\<YYYY-MM>\` on any path that processes the file (success or pending review). It is left intact on hard failure (beets crash, network error).
7. `_MusicStaging\<uuid>\` directory is cleaned up after routing.

The UUID-namespaced subdirectory prevents collisions when processing multiple imports in sequence.

---

### 3.4 Pending Review Sidecar Format

Each file in `E:\Media\MusicPendingReview\` gets an accompanying JSON sidecar. Two review types are supported: `track_review` (low-confidence single file) and `album_review` (folder drop not matched in MusicBrainz).

```json
{
  "review_type": "track_review",
  "original_filename": "01 Some Song.mp3",
  "inbox_archive_path": "E:\\Media\\_MusicInboxArchive\\2026-05\\01 Some Song.mp3",
  "staged_at": "2026-05-19T14:32:00Z",
  "beets_confidence": 0.72,
  "beets_match": {
    "title": "Some Song",
    "artist": "Some Artist",
    "album": "Some Album",
    "year": 2003,
    "track": 1,
    "total_tracks": 12,
    "album_artist": "Some Artist",
    "disc": 1,
    "total_discs": 1,
    "musicbrainz_trackid": "abc123",
    "musicbrainz_albumid": "def456"
  },
  "beets_alternatives": [
    { "title": "Some Song (Live)", "album": "Live at Somewhere", "confidence": 0.51 }
  ],
  "existing_library_duplicate": null,
  "format": "MP3",
  "bitrate": 320,
  "duration_sec": 214,
  "genre_suggestion": "Jazz",
  "genre_normalized": "Jazz",
  "status": "pending"
}
```

For `album_review` type, the sidecar additionally contains:

```json
{
  "review_type": "album_review",
  "folder_name": "Live at Montreux 1975",
  "track_count": 8,
  "tracks": [
    { "filename": "01 Opening.flac", "duration_sec": 180, "format": "FLAC" },
    ...
  ],
  "beets_album_candidates": [],
  "album_override_options": ["import_individually", "import_as_album"],
  "status": "pending"
}
```

The `status` field transitions: `pending` → `confirmed` / `edited` / `rejected`.

---

### 3.5 Admin Dashboard Extension

**Location:** New Flask blueprint (`admin/review_routes.py`), registered in `station_server.py`. All routes require passkey authentication (see §12).

**Routes — track & album review:**

| Route | Method | Description |
|---|---|---|
| `/admin/review` | GET | List all pending-review items (tracks and albums) |
| `/admin/review/<id>` | GET | Detail view for one pending item |
| `/admin/review/<id>/confirm` | POST | Accept beets' suggested tags, trigger import |
| `/admin/review/<id>/edit` | POST | Submit corrected tags, trigger import |
| `/admin/review/<id>/reject` | POST | Delete pending file, write to rejection log |
| `/admin/review/<id>/stream` | GET | Stream audio for in-browser preview |
| `/admin/review/album/<id>/import-individually` | POST | Import album tracks as singletons |
| `/admin/review/album/<id>/import-as-album` | POST | Force import as a grouped album with user-supplied metadata |

**Routes — configuration:**

| Route | Method | Description |
|---|---|---|
| `/admin/config` | GET | View current pipeline config (threshold, archive TTL, etc.) |
| `/admin/config` | POST | Update config values |
| `/admin/genres` | GET | View and manage controlled genre list |
| `/admin/genres` | POST | Add / remove / rename genres |
| `/admin/genres/map` | GET | View genre mapping rules (Last.fm → controlled genre) |
| `/admin/genres/map` | POST | Add or edit a mapping rule |

**Routes — history & health:**

| Route | Method | Description |
|---|---|---|
| `/admin/history` | GET | Rejection log and past import log |
| `/admin/health` | GET | Watcher status, queue depth, failed file count |

Full review workflow details in §6. Genre management details in §11.

---

### 3.6 Post-Import Script (`post_import.py`)

**Purpose:** After a successful beets import (auto or confirmed via dashboard), run the incremental embedding and metadata update.

**Inputs:** A list of absolute file paths that were just imported to `E:\Media\Music\`.

**Steps:**
1. Call `MP3ToVec.py` in single-file incremental mode (see §8).
2. Load existing `mp3tovecs.p`, merge new embedding(s), write back atomically.
3. Update `radio/track_meta.json` with new track metadata entries, atomically.
4. Apply genre normalization: map the beets-suggested genre to the controlled genre list (see §11), write normalized genre back to the file tag and to `track_meta.json`.
5. `POST /reload` to `station_server.py` (see §9).

**Invocation:** Called by the watcher after auto-import success, and by the admin dashboard after confirm/edit actions.

---

### 3.7 Genre Normalization Engine (`import_pipeline/genre_normalizer.py`)

**Purpose:** Map free-form genre strings (from Last.fm via beets, or user input) to the controlled genre list.

**Inputs:** A genre string. **Output:** The best-matching controlled genre string, or `"Unknown"` if no mapping applies.

**Mechanism:** A two-pass lookup:
1. **Exact match** against controlled list (case-insensitive).
2. **Mapping table lookup** (`genre_mappings.json`): e.g. `"Nu Jazz" → "Jazz"`, `"Heavy Metal" → "Metal"`.
3. **Fallback:** `"Unknown"`.

Applied at the end of `post_import.py` and optionally during the library-wide genre normalization sweep (see §11).

---

### 3.8 Inbox Archive Manager (`import_pipeline/archive_manager.py`)

**Purpose:** Manages `E:\Media\_MusicInboxArchive\`, including month-organized archival and 90-day purge.

**Archive operation:** Called after any routing decision that processes a file (success or pending review). Moves inbox original to `_MusicInboxArchive\<YYYY-MM>\<original_filename>`. If a filename collision exists in the archive, appends a short hash suffix.

**Purge operation:** Run as a scheduled task (daily). Walks `_MusicInboxArchive\`, deletes any file whose `mtime` is older than 90 days. Logs purged files to `import_watcher.log`.

---

## 4. File & Folder Layout

### New folders

```
E:\Media\
├── Music\                        (existing main library)
├── MusicInbox\                   (drop zone — watchdog watches this)
├── MusicPendingReview\           (low-confidence staging; album review)
│   ├── <uuid>_01 Some Song.mp3
│   ├── <uuid>_01 Some Song.mp3.json     (sidecar)
│   ├── <uuid>_Live at Montreux 1975\    (folder for album review)
│   │   └── *.flac
│   └── <uuid>_Live at Montreux 1975.json
├── _MusicStaging\                (ephemeral working area, auto-cleaned)
│   └── <uuid>\
│       └── filename.ext
├── _MusicInboxArchive\           (inbox originals held 90 days post-import)
│   └── 2026-05\
│       └── original_filename.flac
└── _MusicFailed\                 (files that errored — for manual inspection)
    ├── filename.ext
    ├── filename.ext.error.json
    └── duplicates\               (replaced files from lossless upgrades)
        └── filename_20260519.mp3
```

### New source files (relative to project root at `C:\Dev`)

```
C:\Dev\
├── import_watcher.py             (watchdog service — Windows Service entry point)
├── import_pipeline\
│   ├── __init__.py
│   ├── watcher.py                (FileSystemEventHandler + quiescence timer)
│   ├── beets_runner.py           (beets subprocess wrapper, confidence parser)
│   ├── staging.py                (staging area management, UUID dirs, cleanup)
│   ├── routing.py                (confidence routing logic)
│   ├── duplicate_check.py        (lossless-preference duplicate logic)
│   ├── post_import.py            (embedding update, metadata update, reload signal)
│   ├── sidecar.py                (pending review sidecar read/write)
│   ├── genre_normalizer.py       (controlled genre mapping engine)
│   └── archive_manager.py        (inbox archive + 90-day purge)
├── admin\
│   ├── review_routes.py          (Flask blueprint — review, config, genres, history)
│   ├── auth_routes.py            (passkey registration + authentication routes)
│   └── templates\
│       ├── review_list.html
│       ├── review_detail.html
│       ├── album_review.html
│       ├── genre_list.html
│       ├── genre_mappings.html
│       ├── config.html
│       └── history.html
├── beets_config\
│   └── config.yaml               (beets config — see §5)
├── data\
│   ├── import_pipeline_config.json   (threshold, archive TTL, etc.)
│   ├── genre_list.json               (controlled genre list — see §11)
│   ├── genre_mappings.json           (Last.fm → controlled genre mapping table)
│   └── passkey_credentials.json      (WebAuthn credential store — see §12)
├── logs\
│   ├── import_watcher.log
│   ├── import_errors.log
│   └── rejection_log.jsonl           (one JSON record per rejected file)
```

### Beets library location

Beets maintains its own SQLite database. Set `directory` to `E:\Media\Music` and `library` to `C:\Dev\beets_config\beets_library.db`. This keeps the beets index separate from `plays.db`.

---

## 5. Beets Configuration

File: `C:\Dev\beets_config\config.yaml`

```yaml
# --- Paths ---
directory: E:/Media/Music
library: C:/Dev/beets_config/beets_library.db

# --- Import behavior ---
import:
  timid: yes           # always prompt/report confidence (we parse stdout)
  write: yes           # write tags to file
  copy: no             # we copy files ourselves; beets MOVEs from staging
  move: yes
  log: C:/Dev/logs/import_watcher.log
  languages: [en]
  quiet: no

# --- Path format ---
paths:
  default: $albumartist/$year - $album%aunique{}/%if{$multidisc,$disc-}$track - $title
  singleton: Singletons/$artist - $title
  comp: Compilations/$year - $album%aunique{}/%if{$multidisc,$disc-}$track - $title

# --- Plugins ---
plugins:
  - duplicates
  - fetchart
  - lastgenre
  - embedart
  - mbsync
  - missing
  - info

# --- Duplicate handling ---
duplicates:
  checksum: yes
  copy: no
  move: no
  delete: no           # we handle replacement via duplicate_check.py
  album: yes

# --- Album art ---
# Decision: store cover.jpg in album directory AND embed a smaller thumbnail.
# cover.jpg serves file manager views and any future web UI that serves art.
# Embedded art ensures portability if files are moved outside the library.
fetchart:
  auto: yes
  sources: filesystem coverart itunes amazon albumart
  maxwidth: 1000
  quality: 75
  store_source: yes    # writes cover.jpg alongside tracks

embedart:
  auto: yes
  maxwidth: 500        # embedded thumbnail — keep small for tag overhead

# --- Genre ---
# lastgenre queries Last.fm and writes a genre tag.
# The raw output is then normalized by genre_normalizer.py (see §11).
# force: yes here so beets writes the Last.fm genre to a temp field;
# genre_normalizer.py maps it to the controlled list and writes the final value.
lastgenre:
  auto: yes
  source: album
  force: yes           # overwrite existing; normalizer runs after and corrects it
  fallback: Unknown
  # Note: lastgenre output is treated as a *suggestion* fed into the normalizer,
  # not the final written genre.

# --- MusicBrainz ---
musicbrainz:
  searchlimit: 5
  extra_tags: [year, catalognum, country, label, media, isrc]

# --- Compilation detection ---
# Handled via the `comp` path template.
# albumartist == "Various Artists" → comp path.

# --- Lossless preference ---
# Handled externally by duplicate_check.py — see §7.
# Beets duplicate plugin flags the collision; we decide what to do.

# --- Multi-disc ---
# $disc prefix in path template handles this automatically.
# multidisc field is set when disc > 1 or totaldiscs > 1.
```

**Notes on path format:**
- `%aunique{}` appends a disambiguation string when two albums share the same name/year.
- For the `disc-` prefix: a track on disc 2 of a multi-disc set becomes `2-05 - Track Title.flac`.

**Invocation mode for confidence parsing:**

Run beets with `--timid` and capture stdout. Beets prints:

```
Finding tags for track "01 Some Song.mp3".
  * Some Song - Some Artist     (96%)
```

The watcher parses the percentage from stdout before committing. Implementation: use `--timid` mode, which outputs candidates and waits for stdin input. Feed `'n'` to abort; on high confidence, re-invoke with `--yes` to auto-accept. The confidence threshold is read from `data/import_pipeline_config.json` at import time so dashboard changes take effect without restarting the watcher.

---

## 6. Manual Review Workflow

### Confidence threshold configuration

The confidence threshold defaults to **90%** and is stored in `data/import_pipeline_config.json`:

```json
{
  "confidence_threshold": 0.90,
  "inbox_archive_ttl_days": 90,
  "staging_cleanup_age_hours": 1
}
```

This file is editable via the admin dashboard at `/admin/config`. Changes take effect on the next import without restarting the watcher.

### What is stored for a pending track

Each pending item in `E:\Media\MusicPendingReview\` has a `.json` sidecar (format in §3.4) containing: original filename, archive path of the inbox original, staging timestamp, beets confidence, top match, up to 3 alternative matches, duplicate status, format/bitrate/duration, genre suggestion and normalized genre, and current status.

### Dashboard list view (`/admin/review`)

Shows a table of all pending items (both `track_review` and `album_review` types):

| Column | Source |
|---|---|
| Type | `review_type` (Track / Album) |
| Filename / Folder | `original_filename` or `folder_name` |
| Confidence | `beets_confidence` (red < 70%, yellow 70–89%, green ≥ 90%) |
| Suggested Title / Album | `beets_match.title` or `folder_name` |
| Suggested Artist | `beets_match.artist` |
| Format | `format` + `bitrate` |
| Staged At | `staged_at` |
| Actions | Confirm / Edit / Reject |

### Detail view — track review (`/admin/review/<id>`)

Shows:
- **Audio preview** — inline `<audio>` element via `/admin/review/<id>/stream`.
- **Beets top match** — full metadata in a read-only block.
- **Alternative matches** — dropdown to switch which match to base edits on.
- **Editable tag form** — all fields editable: title, artist, album, album artist, year, track, total tracks, disc, genre (dropdown limited to controlled genre list — see §11), MusicBrainz IDs.
- **Duplicate warning** — if a library duplicate exists, shows its path, format, and bitrate.
- **Confirm / Edit / Reject** buttons.

### Detail view — album review (`/admin/review/album/<id>`)

Shows:
- Track listing with per-track audio preview links.
- Folder name and detected track count.
- Any partial MusicBrainz candidates beets found (even below threshold).
- Editable album-level metadata form: album title, album artist, year, genre.
- Two import mode buttons:
  - **Import tracks individually** — each track is imported as a singleton (`Singletons/$artist - $title` path). Good for mixed-source folders.
  - **Import as album** — user fills in album metadata, system organizes tracks under `$albumartist/$year - $album/` using the supplied tags. No MusicBrainz lookup; tags are applied directly via mutagen.
- **Reject** button.

### Actions

**Confirm (`POST /admin/review/<id>/confirm`):**
1. Accept beets' top match exactly as-is.
2. Call `beets_runner.py` in apply mode using sidecar data.
3. Beets writes tags and moves file from `MusicPendingReview\` to `E:\Media\Music\`.
4. Call `post_import.py` on the new canonical path (includes genre normalization).
5. Mark sidecar `status: confirmed`; move to `logs/sidecar_archive/`.
6. Redirect to review list with success flash.

**Edit (`POST /admin/review/<id>/edit`):**
1. User has overridden one or more fields in the tag form.
2. `mutagen` applies the corrected tags directly to the file.
3. Compute canonical path from edited tags using the beets path format.
4. Move the file to that path under `E:\Media\Music\`.
5. Call `post_import.py` on the new canonical path.
6. Mark sidecar `status: edited` with final applied tags recorded.

**Reject (`POST /admin/review/<id>/reject`):**
1. Optionally prompt for a short rejection reason (free-text, submitted with the form).
2. Delete the audio file (and folder, for album review) from `MusicPendingReview\`.
3. Append a record to `logs/rejection_log.jsonl`:

```json
{
  "rejected_at": "2026-05-19T15:00:00Z",
  "original_filename": "01 Some Song.mp3",
  "inbox_archive_path": "E:\\Media\\_MusicInboxArchive\\2026-05\\01 Some Song.mp3",
  "beets_suggestion": { "title": "Some Song", "artist": "Some Artist", "confidence": 0.72 },
  "reason": "wrong artist, couldn't identify"
}
```

4. Delete the sidecar file.
5. Redirect to review list. The inbox original remains in `_MusicInboxArchive\` and is purged on schedule (90 days).

---

## 7. Lossless Handling Rules

### Format priority

```
FLAC > ALAC/M4A lossless > WAV/AIFF > MP3 (VBR V0 ≈ 320 CBR) > MP3 lower bitrate > AAC > OGG
```

Lossless is unconditionally preferred over lossy. Among lossless formats, FLAC is preferred (better metadata support, wider compatibility). Among lossy formats, higher bitrate wins.

### Detection

Use `mutagen` to read format and codec:
- `.flac`: always lossless.
- `.m4a`/`.mp4`: check codec. `alac` → lossless; `aac` → lossy. Inspect via `mutagen.mp4.MP4Info.codec`.
- `.wav`, `.aiff`: lossless (uncompressed); treated as lossless for preference purposes.
- `.mp3`: always lossy; compare via `info.bitrate`.

### Duplicate collision rules (`duplicate_check.py`)

When beets detects a file already exists in the library:

| Incoming | Existing | Action |
|---|---|---|
| Lossless | Lossy | Replace existing with incoming. Archive old file to `_MusicFailed\duplicates\`. |
| Lossless | Lossless (same format) | Checksum compare. Identical → reject incoming as redundant. Different rip → flag for manual review. |
| Lossless | Lossless (different format) | Prefer FLAC > ALAC > WAV/AIFF; replace if incoming is higher priority. |
| Lossy | Lossless | Reject incoming. Keep existing. |
| Lossy | Lossy (same format) | Keep higher bitrate. If equal, keep existing. |
| Lossy | Lossy (different format) | Apply format priority. Replace if incoming is higher priority format. |

**Replace procedure:**
1. Move existing file to `E:\Media\_MusicFailed\duplicates\<filename>_<YYYYMMDD>.<ext>` as a backup.
2. Import incoming file to canonical path.
3. Remove the old path key from `mp3tovecs.p`.
4. Remove old entry from `track_meta.json`.
5. Run incremental embedding on the new file via `post_import.py`.

**Flagging for review:** Lossless-vs-lossless with a different rip (and non-identical checksum) routes to `MusicPendingReview\` with `existing_library_duplicate` populated in the sidecar, showing both files' formats, sizes, and paths.

---

## 8. Incremental Embedding & Cache Update

### Integration with the recommendation system

All new imports are incrementally added to `mp3tovecs.p` — the same pickle the recommendation system reads at startup. This means newly imported tracks become immediately eligible for similarity-based recommendations after the next server reload, with no separate indexing step. The embedding key format is **absolute paths** (e.g. `E:\Media\Music\Artist\2003 - Album\01 - Title.flac`), consistent with the existing system.

### Required refactoring of `MP3ToVec.py`

Currently `MP3ToVec.py` scans the entire library on every run. Add an incremental CLI mode:

```
python MP3ToVec.py --incremental "E:\Media\Music\Artist\2003 - Album\01 - Title.flac"
```

This mode:
- Skips the full directory scan.
- Computes embeddings only for the given absolute paths.
- Writes results to a temp file (`mp3tovecs_new.p.tmp`) for `post_import.py` to merge.

Preferred refactor: extract the core embedding function into a module (`mp3tovec_core.py`) importable by `post_import.py`:

```python
from mp3tovec_core import compute_embedding

embedding = compute_embedding(r"E:\Media\Music\Artist\2003 - Album\01 - Title.flac")
```

### Merging into `mp3tovecs.p`

```python
import pickle, os

# Load existing
with open("mp3tovecs.p", "rb") as f:
    existing = pickle.load(f)  # dict: {absolute_path_str: embedding_vector}

# Remove old path if this is a replacement
if old_path and old_path in existing:
    del existing[old_path]

# Add new embedding (key is absolute path string)
existing[new_canonical_path] = new_embedding

# Atomic write
with open("mp3tovecs.p.tmp", "wb") as f:
    pickle.dump(existing, f)
os.replace("mp3tovecs.p.tmp", "mp3tovecs.p")
```

`os.replace` is atomic on Windows NTFS, preventing corrupt reads if the server reloads mid-write.

### Updating `radio/track_meta.json`

```python
import json, os

with open("radio/track_meta.json", "r", encoding="utf-8") as f:
    meta = json.load(f)  # dict: {absolute_path_str: {title, artist, ...}}

meta[new_canonical_path] = {
    "title": tags.title,
    "artist": tags.artist,
    "album": tags.album,
    "year": tags.year,
    "duration": tags.duration,
    "genre": normalized_genre,   # controlled-list genre, not raw Last.fm value
    # match existing schema field names exactly
}

# Atomic write
with open("radio/track_meta.json.tmp", "w", encoding="utf-8") as f:
    json.dump(meta, f, indent=2)
os.replace("radio/track_meta.json.tmp", "radio/track_meta.json")
```

**Before implementing:** Read two or three entries from the existing `track_meta.json` to confirm the exact field names and structure.

---

## 9. Server Reload

### Decision: HTTP `/reload` endpoint

**Chosen approach:** `POST /reload` HTTP endpoint on `station_server.py`. This is the correct choice for Windows — `SIGHUP` and `SIGUSR1` are not available on Windows, and named pipe or `CTRL_C_EVENT` alternatives are significantly more complex to reason about and test. The HTTP endpoint is observable (returns JSON with the new track count), straightforward to secure with a token, and works identically regardless of how the server process is started.

### Implementation

Add to `station_server.py`:

```python
import threading, pickle, json, os

_reload_lock = threading.Lock()

@app.route("/reload", methods=["POST"])
def reload_data():
    token = request.headers.get("X-Reload-Token")
    if token != os.environ.get("RELOAD_TOKEN"):
        abort(403)

    with _reload_lock:
        global mp3tovecs
        with open("mp3tovecs.p", "rb") as f:
            mp3tovecs = pickle.load(f)

        global track_meta
        with open("radio/track_meta.json", "r", encoding="utf-8") as f:
            track_meta = json.load(f)

    return jsonify({"status": "reloaded", "tracks": len(mp3tovecs)})
```

`post_import.py` calls this after the atomic file writes complete:

```python
import requests, os

resp = requests.post(
    "http://localhost:5000/reload",
    headers={"X-Reload-Token": os.environ["RELOAD_TOKEN"]},
    timeout=10
)
resp.raise_for_status()
```

**Token storage:** `RELOAD_TOKEN` lives in a `.env` file loaded by both the server and the watcher at startup. Do not hardcode.

**Thread safety:** `_reload_lock` prevents a reload from interleaving with an in-flight request that reads the globals. The GIL makes the `global` reassignment atomic, but the lock is still correct practice.

**Failure handling:** If the reload endpoint returns an error or times out, `post_import.py` logs a warning but does not fail the import. The updated files are on disk; the server will pick them up on next restart. The endpoint is a best-effort live-reload, not a hard dependency.

---

## 10. Error Handling & Rollback

### Failure scenarios and responses

| Stage | Failure | Response |
|---|---|---|
| File detection | File still being written (partial) | Stabilization delay (3s); retry up to 3 times |
| Copy to staging | Disk full, permission error | Log error, leave inbox file intact, alert |
| Beets import | MusicBrainz unreachable (timeout) | Move staged copy to `_MusicFailed\`, write error sidecar, leave inbox intact |
| Beets import | File unreadable / corrupt audio | Move to `_MusicFailed\`, write error sidecar, leave inbox intact |
| Embedding computation | MP3ToVec fails on specific file | Do NOT archive inbox file; do NOT update `mp3tovecs.p`; log error; leave tagged file in place for manual retry |
| `mp3tovecs.p` write | Disk full mid-write | Atomic replace means old file is intact; log error; retry next import cycle |
| `track_meta.json` write | Same | Same atomic-replace safety |
| Server reload | `/reload` timeout or server down | Log warning; files on disk are correct; reload on next server restart |
| Archive operation | Disk full when archiving inbox original | Log error, leave inbox file in place (do not delete) |

### Rollback design

True transactional rollback is complex. Instead, the pipeline is **idempotent and recoverable**:

1. **Inbox file is not archived until a routing decision is made.** Hard failures (beets crash, network error) leave the inbox file in place so the watcher retries on next startup.
2. **Staging area is ephemeral.** A startup cleanup pass removes stale `_MusicStaging\` directories older than 1 hour (configured in `import_pipeline_config.json`).
3. **`mp3tovecs.p` and `track_meta.json` use atomic writes.** No partial state can corrupt existing data.
4. **Beets database is separate from `plays.db`.** Beets failures do not touch play history.
5. **`_MusicFailed\`** is human-inspectable. Files there can be re-queued by moving them back into `MusicInbox`.
6. **Archive originals serve as last-resort recovery.** If a bad import slips through and the original is needed, it's in `_MusicInboxArchive\` for up to 90 days.

### Startup cleanup

`import_watcher.py` on startup:
- Scans `_MusicStaging\` for directories older than the configured age → removes them.
- Logs any files in `_MusicFailed\` that haven't been addressed.
- Logs count of items in `MusicPendingReview\`.
- Logs count of files in `_MusicInboxArchive\` eligible for purge within the next 7 days (early warning).

---

## 11. Genre Management

### Controlled genre list

The genre list is stored in `data/genre_list.json` — a simple ordered array of canonical genre strings:

```json
["Ambient", "Blues", "Classical", "Electronic", "Folk", "Hip-Hop", "Jazz",
 "Metal", "Pop", "Punk", "R&B", "Rock", "Soul", "Soundtrack", "World", "Unknown"]
```

This list is user-managed via the admin dashboard at `/admin/genres`. Users can add, rename, and remove genres. Removing a genre triggers a prompt: "N tracks use this genre. Reassign to: [dropdown]."

**No free-form genre tags are written to the library.** Every genre tag on every file must be a member of this list.

### Genre mapping table

`data/genre_mappings.json` maps incoming genre strings (from Last.fm, MusicBrainz, or user imports) to controlled genres:

```json
{
  "Nu Jazz": "Jazz",
  "Acid Jazz": "Jazz",
  "Heavy Metal": "Metal",
  "Death Metal": "Metal",
  "Indie Rock": "Rock",
  "Indie Pop": "Pop",
  "Trip-Hop": "Electronic",
  "IDM": "Electronic",
  "Gangsta Rap": "Hip-Hop"
}
```

Mappings are editable at `/admin/genres/map`. Any string not in either the controlled list or the mapping table resolves to `"Unknown"`.

### Normalization pipeline

After each import, `genre_normalizer.py` is called by `post_import.py`:

1. Read the genre tag beets wrote to the file (from Last.fm).
2. Look up in mapping table → controlled genre.
3. Write the normalized genre back to the file tag (via `mutagen`).
4. Store normalized genre in `track_meta.json`.
5. Log the raw → normalized mapping for auditing.

### Library-wide genre normalization sweep

A one-time sweep (and periodic re-sweep when mappings change) normalizes existing library files. This is a separate script (`scripts/normalize_genres.py`) that:

1. Reads all entries from `track_meta.json`.
2. For each, reads the genre tag from the file.
3. Applies normalization.
4. If normalized genre differs from stored genre: writes corrected tag to file, updates `track_meta.json`.
5. Reports a summary: N files updated, breakdown by old → new genre mapping.

This script is non-destructive: it only writes if the normalization changes the value. It should be run once after the controlled list is finalized, and re-run whenever mapping rules change.

### Admin dashboard genre management

`/admin/genres` shows:
- The controlled genre list with track counts per genre.
- Add / Rename / Remove buttons. Remove prompts for reassignment.

`/admin/genres/map` shows:
- Table of all mapping rules (source → target).
- Add new rule form.
- Edit / Delete per rule.
- "Run normalization sweep now" button (queues the sweep script).

---

## 12. Admin Dashboard Authentication (WebAuthn / Passkeys)

### Decision

The admin dashboard uses **WebAuthn (passkey standard)** for authentication. This provides phishing-resistant, password-free login tied to the user's device (Windows Hello, Touch ID, hardware key, etc.). All `/admin/*` routes require a valid WebAuthn session.

This is a new build requirement, not an extension of any existing auth mechanism.

### Technology

- **Server-side:** `py_webauthn` library (Python implementation of WebAuthn). Handles registration and authentication ceremony logic.
- **Client-side:** Native browser WebAuthn API (`navigator.credentials.create` / `navigator.credentials.get`) — no external JS library needed.
- **Credential store:** `data/passkey_credentials.json` — stores registered credential IDs, public keys, and sign counts. Fine for a single-user local deployment; replace with a proper database if multi-user is ever needed.
- **Session:** Flask-Session with a server-side session store (filesystem or Redis). Session cookie is `HttpOnly`, `Secure`, `SameSite=Strict`.

### Routes

| Route | Method | Description |
|---|---|---|
| `/admin/login` | GET | Login page — "Sign in with passkey" button |
| `/admin/auth/challenge` | POST | Generate and return a WebAuthn challenge (stored in session) |
| `/admin/auth/verify` | POST | Verify credential response; set session on success |
| `/admin/auth/logout` | POST | Clear session |
| `/admin/register` | GET | Registration page (only accessible from localhost or if no credentials exist yet) |
| `/admin/register/challenge` | POST | Generate registration challenge |
| `/admin/register/verify` | POST | Verify and store new credential |

### Registration flow (first-time setup)

1. Navigate to `/admin/register` from localhost (route is IP-restricted to `127.0.0.1` after first credential is registered).
2. Browser prompts to create a passkey via Windows Hello (or platform authenticator).
3. Server stores credential ID, public key, and initial sign count in `passkey_credentials.json`.
4. Redirect to `/admin/login`.

### Authentication flow

1. User navigates to any `/admin/*` route → redirected to `/admin/login` if no valid session.
2. Login page calls `POST /admin/auth/challenge` to get a challenge from the server.
3. Browser calls `navigator.credentials.get()` → Windows Hello prompt.
4. Browser posts signed assertion to `POST /admin/auth/verify`.
5. Server verifies signature against stored public key; checks sign count for replay protection.
6. On success: set `session["authenticated"] = True`; redirect to original URL.

### Session protection

```python
from functools import wraps
from flask import session, redirect, url_for, request

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("auth.login", next=request.url))
        return f(*args, **kwargs)
    return decorated
```

Apply `@require_auth` to all `/admin/*` route handlers.

### Multi-credential support

`passkey_credentials.json` supports storing multiple credentials (e.g. laptop + desktop + hardware key). Registration of additional credentials requires an existing authenticated session. This is the mechanism for adding a backup authenticator.

### `/reload` endpoint auth

The `/reload` endpoint uses its own token-based auth (see §9), not WebAuthn sessions. This is intentional — `post_import.py` calls it server-to-server, not from a browser.

---

## 13. Phased Build Order

### Phase 1 — Foundation

**Goal:** Watcher running, beets configured, high-confidence files landing in the library. No embedding or dashboard yet.

1. Install beets. Test `beets_config/config.yaml` manually with sample files. Validate path format, fetchart, lastgenre, duplicates plugins.
2. Write `import_pipeline/beets_runner.py` — subprocess wrapper, confidence parser.
3. Write `import_pipeline/staging.py` — UUID staging dirs, cleanup.
4. Write `import_pipeline/routing.py` — confidence threshold from config file, route to Music / PendingReview / Failed.
5. Write `import_pipeline/archive_manager.py` — archive inbox originals; stub out 90-day purge (implement in Phase 5).
6. Write `import_watcher.py` entry point (individual file handling; quiescence timer for folders is Phase 5).
7. Test end-to-end: drop file in MusicInbox → lands in Music or PendingReview, inbox original moves to archive.

### Phase 2 — Incremental embedding

**Goal:** Imported files become playable and recommendation-eligible.

8. Refactor `MP3ToVec.py` to support single-file incremental mode.
9. Write `import_pipeline/post_import.py` — embedding update, `track_meta.json` update.
10. Add `POST /reload` endpoint to `station_server.py` with token auth.
11. Wire `post_import.py` into the watcher (called after successful auto-import).
12. Test: import a file, verify it appears in station playback and recommendations.

### Phase 3 — Manual review dashboard (no auth yet)

**Goal:** Pending-review files are manageable via UI. Auth is added in Phase 6 — run on localhost only during this phase.

13. Write `sidecar.py`.
14. Write `admin/review_routes.py` blueprint: list / detail / confirm / edit / reject for track review.
15. Write `review_list.html`, `review_detail.html`. Add audio preview streaming route.
16. Register blueprint in `station_server.py`.
17. Wire confirm/edit through to `post_import.py`.
18. Test: low-confidence file reviewed and confirmed; appears in station.

### Phase 4 — Genre management

**Goal:** Controlled genre list is live; all imports get normalized genres.

19. Create `data/genre_list.json` with initial genre set.
20. Create `data/genre_mappings.json` with initial mapping rules.
21. Write `import_pipeline/genre_normalizer.py`.
22. Wire genre normalization into `post_import.py`.
23. Write `scripts/normalize_genres.py` for library-wide sweep.
24. Write `admin/genre_routes.py`: `/admin/genres` and `/admin/genres/map` views.
25. Run library-wide sweep on existing tracks.
26. Test: import a file with a mapped genre (e.g. "Nu Jazz" → "Jazz"); verify tag and `track_meta.json`.

### Phase 5 — Lossless & duplicate handling

**Goal:** Lossless upgrades work correctly.

27. Write `import_pipeline/duplicate_check.py` — format priority, bitrate comparison, lossless detection.
28. Wire duplicate check into routing, before beets commit.
29. Implement replace procedure: backup old file, update pickle and JSON, import new.
30. Test all collision cases in §7 table.

### Phase 6 — Passkey authentication

**Goal:** Admin dashboard is secured before any network exposure.

31. Install `py_webauthn` and `Flask-Session`.
32. Write `admin/auth_routes.py`: registration and authentication ceremonies.
33. Write `login.html` and `register.html` templates.
34. Apply `@require_auth` decorator to all `/admin/*` routes.
35. Perform first-time registration from localhost; verify login via Windows Hello.
36. Test: unauthenticated request to `/admin/review` redirects to login; authenticated session persists correctly.
37. Register a second credential (backup authenticator).

### Phase 7 — Hardening & album import

**Goal:** Reliability for unattended operation; album folder drop support.

38. Implement quiescence timer for folder drop detection in `watcher.py`.
39. Write `album_review.html` template; wire `import-individually` and `import-as-album` routes.
40. Implement 90-day archive purge in `archive_manager.py`; schedule as a Windows Task Scheduler daily job.
41. Implement startup cleanup scan of `_MusicStaging\`.
42. Install `import_watcher.py` as a Windows Service via `pywin32`.
43. Add structured logging (Python `logging` with rotating file handler).
44. Add failure alerting (Windows desktop notification via `win10toast` or email via `smtplib`).
45. Write health-check: `python import_watcher.py --check` reports queue depth, failed files, pending review count, server reload status.

---

*End of plan. All architectural decisions are resolved. Next step: Phase 1 — beets installation and config validation before writing any watcher code.*
