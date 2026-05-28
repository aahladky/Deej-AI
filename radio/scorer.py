"""
scorer.py — Behavioral re-ranking layer for DeeJAI

Two-level scoring:
  1. Track-level  — per-song completion rate (kernel-weighted by time-of-day)
                    plus recency from plays.db. Matched by normalised
                    (artist, title) so Spotify history rows line up with
                    local file metadata.
  2. Artist-level — fallback when a track has fewer than MIN_PLAYS_FOR_SIGNAL
                    plays. Adds time-of-day affinity (artists you tend to
                    reach for around the current hour).

Time-of-day model
------------------
The completion rate is computed by weighting each historical play by a Gaussian
kernel on the *circular* distance between that play's local hour and the query
hour. This replaces the old binary gym/home context: it captures the full
completion-vs-hour curve smoothly, has no bucket boundaries, and degrades
gracefully — when a given hour is sparsely sampled for a track, the rate shrinks
back toward that track's all-time rate (Bayesian pseudo-count).

The stored `context` column (gym/home) is left intact: callers still write it
and the admin dashboard still groups by it. It simply no longer drives scoring.
`get_context()` is therefore preserved for back-compat. Scoring methods accept a
`when` argument that may be None (→ current clock hour), a numeric hour-of-day
(for previews / DJ seeding at a specific time), or a legacy context string
(treated as "now").

Combined score per candidate:
    score = AUDIO_W * audio_sim + BEH_W * behavioral
"""

import sqlite3, time, datetime, math, os, re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Tuple, Union

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

import os as _os
DB_PATH   = _os.environ.get('DEEJAY_DB_PATH', r'C:\Dev\DeeJAI\radio\plays.db')

AUDIO_W   = 0.40
BEH_W     = 0.60

RECENCY_HALF_LIFE_DAYS = 30
MIN_PLAYS_FOR_SIGNAL   = 3
REFRESH_EVERY_N_PLAYS  = 25

# Time-of-day kernel
SIGMA_HOURS    = 2.5   # Gaussian bandwidth (hours) on the 24h circle
TIME_SHRINKAGE = 4.0   # pseudo-count (in weighted plays): how much hour-local
                       # evidence is needed before it overrides the all-time rate

# Age decay — taste shifts over time, so older plays count less. Each play is
# weighted by 0.5 ** (age_days / AGE_HALF_LIFE_DAYS) when building stats, so the
# completion rates and hour histograms reflect *current* taste. (Aaron's taste
# turned over ~2020 indie/rap → country/rock; old-era plays should fade out.)
AGE_HALF_LIFE_DAYS = 540.0   # ~18 months
AGE_WEIGHT_NO_TS   = 0.10    # weight for rows with no usable timestamp

DEFAULT_COMPLETION_RATE = 0.55
DEFAULT_RECENCY         = 0.50
DEFAULT_CTX_AFFINITY    = 0.50

HOURS = 24

# Legacy gym/home — retained ONLY for get_context() / the stored context label
# and the admin dashboard. These no longer affect scoring.
CTX_GYM  = 'gym'
CTX_HOME = 'home'
GYM_DAYS    = {0, 1, 2, 3, 4}
GYM_START_H = 16.5
GYM_END_H   = 21.0

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def get_context() -> str:
    """Legacy gym/home label. Still written to the `context` column by callers
    and shown in the admin dashboard's context split. No longer used in scoring."""
    now  = datetime.datetime.now()
    hour = now.hour + now.minute / 60.0
    if now.weekday() in GYM_DAYS and GYM_START_H <= hour <= GYM_END_H:
        return CTX_GYM
    return CTX_HOME


def now_hour() -> float:
    """Current local hour-of-day as a float in [0, 24)."""
    now = datetime.datetime.now()
    return now.hour + now.minute / 60.0


@lru_cache(maxsize=256)
def _kernel_weights(hour_q: float) -> Tuple[float, ...]:
    """Gaussian weights over the 24 integer hour-bins for a query hour, using
    circular (wrap-around-midnight) distance. Cached by quantised hour."""
    ws = []
    for h in range(HOURS):
        d = abs(h - hour_q)
        d = min(d, HOURS - d)            # wrap around midnight (e.g. 23↔1)
        ws.append(math.exp(-(d * d) / (2.0 * SIGMA_HOURS * SIGMA_HOURS)))
    return tuple(ws)


def _quantise_hour(hour: float) -> float:
    """Round to the nearest 15 minutes so _kernel_weights caches effectively."""
    return round((hour % 24.0) * 4) / 4.0


def _resolve_hour(when: Union[None, float, int, str]) -> float:
    """Accept None (→ now), a numeric hour-of-day, or a legacy context string
    (→ now, for back-compat). Returns a float hour in [0, 24)."""
    if when is None:
        return now_hour()
    if isinstance(when, bool):           # guard: bools are ints in Python
        return now_hour()
    if isinstance(when, (int, float)):
        return float(when) % 24.0
    return now_hour()                    # legacy 'gym'/'home'/'' string → clock


def _recency(last_played_at: float) -> float:
    if last_played_at == 0.0:
        return DEFAULT_RECENCY
    days_ago = (time.time() - last_played_at) / 86400.0
    return math.exp(-math.log(2) * days_ago / RECENCY_HALF_LIFE_DAYS)


def _age_weight(started_at: float) -> float:
    """How much a play counts toward the stats, decaying with its age so that
    recent listening dominates. Plays with no usable timestamp get a small
    fixed weight so they still contribute marginally."""
    if not started_at:
        return AGE_WEIGHT_NO_TS
    age_days = (time.time() - started_at) / 86400.0
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / AGE_HALF_LIFE_DAYS)


_PUNCT = re.compile(r"[^\w\s]")
_WS    = re.compile(r"\s+")

def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.
    Used to match Spotify track names against local file tags."""
    s = (s or '').lower().strip()
    s = _PUNCT.sub('', s)
    s = _WS.sub(' ', s).strip()
    return s


def _hour_of(started_at: float) -> Optional[int]:
    """Local hour-of-day (0-23) for a unix timestamp, or None if invalid.
    Uses local time, matching get_context()/now_hour() on the host machine."""
    if not started_at:
        return None
    try:
        return datetime.datetime.fromtimestamp(started_at).hour
    except (ValueError, OSError, OverflowError):
        return None


# --------------------------------------------------------------------------- #
# Stats dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class _BaseStats:
    # Raw counts — used only for gating (MIN_PLAYS_FOR_SIGNAL) and reporting.
    total_plays:       int   = 0
    total_completions: int   = 0
    last_played_at:    float = 0.0
    # Age-weighted accumulators — used for all rate computations, so recent
    # listening dominates and old-era taste fades out.
    w_total_plays:       float = 0.0
    w_total_completions: float = 0.0
    # 24-slot age-weighted histograms of plays / completions by local hour.
    plays_by_hour:       List[float] = field(default_factory=lambda: [0.0] * HOURS)
    completions_by_hour: List[float] = field(default_factory=lambda: [0.0] * HOURS)

    def _ingest(self, completed: int, started_at: float):
        completed = int(completed or 0)
        w = _age_weight(started_at)
        # raw (unweighted) — gating / reporting
        self.total_plays       += 1
        self.total_completions += completed
        if started_at and started_at > self.last_played_at:
            self.last_played_at = started_at
        # age-weighted — rates
        self.w_total_plays       += w
        self.w_total_completions += w * completed
        h = _hour_of(started_at)
        if h is not None:
            self.plays_by_hour[h]       += w
            self.completions_by_hour[h] += w * completed

    def _alltime_rate(self) -> float:
        """Age-weighted all-time completion rate (falls back to raw, then default)."""
        if self.w_total_plays > 1e-9:
            return self.w_total_completions / self.w_total_plays
        if self.total_plays > 0:
            return self.total_completions / self.total_plays
        return DEFAULT_COMPLETION_RATE

    def completion_rate_at(self, hour: float) -> float:
        """Kernel-weighted completion rate around `hour`, shrunk toward this
        object's (age-weighted) all-time rate when the hour is sparsely sampled."""
        w = _kernel_weights(_quantise_hour(hour))
        num = 0.0
        den = 0.0
        pbh = self.plays_by_hour
        cbh = self.completions_by_hour
        for h in range(HOURS):
            ph = pbh[h]
            if ph:
                num += w[h] * cbh[h]
                den += w[h] * ph
        alltime = self._alltime_rate()
        # Bayesian shrinkage toward the all-time rate (TIME_SHRINKAGE pseudo-plays).
        # When den >> TIME_SHRINKAGE the local rate dominates; when the hour is
        # thin, the estimate falls back to the track's all-time behaviour.
        return (num + TIME_SHRINKAGE * alltime) / (den + TIME_SHRINKAGE)

    def recency_score(self) -> float:
        return _recency(self.last_played_at)


@dataclass
class TrackStats(_BaseStats):
    """Per-song stats. No affinity term — a track's sound doesn't change."""
    artist: str = ''
    title:  str = ''

    def behavioral_score(self, hour: float) -> float:
        return self.completion_rate_at(hour) * self.recency_score()


@dataclass
class ArtistStats(_BaseStats):
    """Per-artist stats. Includes time-of-day affinity (do you reach for this
    artist around this hour?), the time analogue of the old gym/home skew."""
    artist: str = ''

    def time_affinity(self, hour: float) -> float:
        """How over-represented this artist's plays are around `hour` relative
        to a uniform spread across the day. Returns a multiplier in [0.25, 1.0],
        mirroring the shape of the old context_affinity."""
        if self.total_plays < MIN_PLAYS_FOR_SIGNAL:
            return DEFAULT_CTX_AFFINITY
        kw = _kernel_weights(_quantise_hour(hour))
        mean_w = sum(kw) / HOURS
        if mean_w <= 0 or self.w_total_plays <= 1e-9:
            return DEFAULT_CTX_AFFINITY
        expected = self.w_total_plays * mean_w        # weighted count if spread evenly
        actual   = sum(kw[h] * self.plays_by_hour[h] for h in range(HOURS))
        ratio    = (actual / expected) if expected > 0 else 1.0
        return 0.25 + 0.75 * min(1.0, ratio / 2.0)

    def behavioral_score(self, hour: float) -> float:
        return (
            self.completion_rate_at(hour)
            * self.recency_score()
            * self.time_affinity(hour)
        )


# --------------------------------------------------------------------------- #
# Scorer
# --------------------------------------------------------------------------- #

class Scorer:
    """
    Loads per-track and per-artist behavioral stats from plays.db and
    re-ranks audio similarity candidates from DeeJAI.

    Scoring priority:
        1. Track-level stats (matched by normalised artist+title)
           if track has >= MIN_PLAYS_FOR_SIGNAL plays.
        2. Artist-level stats as fallback.

    Usage:
        scorer = Scorer()
        best   = scorer.rescore_candidates(candidates, track_meta)
        # after each logged play:
        scorer.on_play_logged()

    Scoring is driven by the current clock hour. Pass `when=<hour float>` to any
    scoring method to evaluate "as if" it were a specific time of day (used for
    DJ seeding and admin previews).
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._artist_stats: Dict[str, ArtistStats]            = {}
        self._track_stats:  Dict[Tuple[str, str], TrackStats] = {}
        self._plays_since_refresh = 0
        self._ensure_db()
        self.refresh()

    def _ensure_db(self):
        con = sqlite3.connect(self.db_path)
        con.execute('''CREATE TABLE IF NOT EXISTS plays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_path TEXT, artist TEXT, title TEXT, album TEXT,
            started_at REAL, ms_played INTEGER, completed INTEGER, context TEXT)''')
        con.commit(); con.close()

    def refresh(self):
        """Re-query plays.db and rebuild both track- and artist-level stats."""
        if not os.path.exists(self.db_path):
            return
        con = sqlite3.connect(self.db_path)
        rows = con.execute('''
            SELECT artist, title, completed, started_at
            FROM plays
            WHERE artist IS NOT NULL AND artist != ""
        ''').fetchall()
        con.close()

        artist_stats: Dict[str, ArtistStats]            = {}
        track_stats:  Dict[Tuple[str, str], TrackStats] = {}

        for artist, title, completed, started_at in rows:
            title = title or ''

            # ── artist level ──
            if artist not in artist_stats:
                artist_stats[artist] = ArtistStats(artist=artist)
            artist_stats[artist]._ingest(completed, started_at)

            # ── track level ──
            key = (_normalize(artist), _normalize(title))
            if key not in track_stats:
                track_stats[key] = TrackStats(artist=artist, title=title)
            track_stats[key]._ingest(completed, started_at)

        self._artist_stats = artist_stats
        self._track_stats  = track_stats
        self._plays_since_refresh = 0

    def on_play_logged(self):
        self._plays_since_refresh += 1
        if self._plays_since_refresh >= REFRESH_EVERY_N_PLAYS:
            self.refresh()

    def _behavioral(self, artist: str, title: str, hour: float) -> float:
        """Return best available behavioral score for this artist+title."""
        key = (_normalize(artist), _normalize(title))
        ts  = self._track_stats.get(key)
        if ts and ts.total_plays >= MIN_PLAYS_FOR_SIGNAL:
            return ts.behavioral_score(hour)
        # fall back to artist level
        ast = self._artist_stats.get(artist)
        if ast and ast.total_plays >= MIN_PLAYS_FOR_SIGNAL:
            return ast.behavioral_score(hour)
        return DEFAULT_COMPLETION_RATE * DEFAULT_RECENCY * DEFAULT_CTX_AFFINITY

    def score_candidate(
        self,
        path:       str,
        audio_sim:  float,
        track_meta: dict,
        when:       Union[None, float, int, str] = None,
    ) -> float:
        hour   = _resolve_hour(when)
        meta   = track_meta.get(path, {})
        artist = meta.get('artist', '')
        title  = meta.get('title',  '')
        beh    = self._behavioral(artist, title, hour)
        return AUDIO_W * audio_sim + BEH_W * beh

    def rescore_candidates(
        self,
        candidates:  List[Tuple[str, float]],
        track_meta:  dict,
        when:        Union[None, float, int, str] = None,
    ) -> str:
        if not candidates:
            raise ValueError("candidates list is empty")
        hour = _resolve_hour(when)
        scored = [
            (path, self.score_candidate(path, sim, track_meta, hour))
            for path, sim in candidates
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0]

    # ------------------------------------------------------------------ #
    # Debug helpers
    # ------------------------------------------------------------------ #

    def top_tracks(self, when: Union[None, float, int, str] = None,
                   n: int = 20) -> List[Tuple[str, str, float]]:
        """Top-N tracks by behavioral score at `when`, only those with enough plays."""
        hour = _resolve_hour(when)
        ranked = [
            (ts.artist, ts.title, ts.behavioral_score(hour))
            for ts in self._track_stats.values()
            if ts.total_plays >= MIN_PLAYS_FOR_SIGNAL
        ]
        ranked.sort(key=lambda x: x[2], reverse=True)
        return ranked[:n]

    def top_artists(self, when: Union[None, float, int, str] = None,
                    n: int = 20) -> List[Tuple[str, float]]:
        hour = _resolve_hour(when)
        ranked = [
            (a, s.behavioral_score(hour))
            for a, s in self._artist_stats.items()
        ]
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked[:n]

    def track_report(self, artist: str, title: str,
                     when: Union[None, float, int, str] = None) -> str:
        hour = _resolve_hour(when)
        key  = (_normalize(artist), _normalize(title))
        ts   = self._track_stats.get(key)
        ast  = self._artist_stats.get(artist)
        lines = [f"Track  : {artist} — {title}",
                 f"Key    : {key}",
                 f"Hour   : {hour:.2f}"]
        if ts:
            peak_h = max(range(HOURS), key=lambda h: ts.plays_by_hour[h]) \
                     if ts.total_plays else 0
            lines += [
                f"  Track plays    : {ts.total_plays}  (completions: {ts.total_completions})",
                f"  All-time rate  : {ts._alltime_rate():.3f}",
                f"  Rate @ {hour:04.1f}h   : {ts.completion_rate_at(hour):.3f}",
                f"  Busiest hour   : {peak_h:02d}:00",
                f"  Recency        : {ts.recency_score():.3f}",
                f"  Score @ {hour:04.1f}h  : {ts.behavioral_score(hour):.3f}",
            ]
        else:
            lines.append("  No track-level data (will use artist fallback)")
        if ast:
            lines.append(f"  Artist score   : {ast.behavioral_score(hour):.3f} "
                         f"(affinity {ast.time_affinity(hour):.2f}, {ast.total_plays} plays)")
        return "\n".join(lines)

    def stats_summary(self) -> str:
        n_tracks_with_data = sum(
            1 for ts in self._track_stats.values()
            if ts.total_plays >= MIN_PLAYS_FOR_SIGNAL
        )
        return (f"Artists : {len(self._artist_stats)}\n"
                f"Tracks  : {len(self._track_stats)} total, "
                f"{n_tracks_with_data} with >={MIN_PLAYS_FOR_SIGNAL} plays")


# --------------------------------------------------------------------------- #
# CLI:  python scorer.py [--hour H] [Artist -- Title]
# --------------------------------------------------------------------------- #

if __name__ == '__main__':
    import sys
    args = sys.argv[1:]

    when: Optional[float] = None
    if '--hour' in args:
        i = args.index('--hour')
        try:
            when = float(args[i + 1])
        except (IndexError, ValueError):
            print("Usage: python scorer.py [--hour H] [Artist -- Title]")
            sys.exit(1)
        del args[i:i + 2]

    scorer = Scorer()
    print(scorer.stats_summary(), "\n")
    hour = _resolve_hour(when)
    print(f"Query hour: {hour:.2f}\n")
    print(f"Top 20 tracks @ {hour:.1f}h:")
    for artist, title, score in scorer.top_tracks(when, 20):
        print(f"  {score:.3f}  {artist} — {title}")

    if args:
        arg = " ".join(args)
        if ' -- ' in arg:
            a, t = arg.split(' -- ', 1)
            print("\n" + scorer.track_report(a.strip(), t.strip(), when))
        else:
            print("\nUsage: python scorer.py [--hour H] [Artist -- Title]")
