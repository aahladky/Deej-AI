"""
deejay_api.py — DeeJAI Smart Curator REST API  (port 8050)

Headless Flask API serving music recommendations to the React Native iOS client.
Audio delivery is handled by Navidrome; DeeJAI owns only the "what plays next" logic.

Endpoints:
    GET  /api/health      — liveness + track count
    GET  /api/recommend   — seed-based ranked recommendations
    POST /api/played      — record a play/skip event + update ring buffer
    GET  /api/home        — home screen payload (top artists, seeds, recent)

Run:
    python deejay_api.py
"""

import json
import math
import os
import pickle
import random
import re
import shutil
import sqlite3
import sys
import time
from collections import deque

import numpy as np
from flask import Flask, jsonify, request
# ── import scorer from same directory ──────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scorer import Scorer, get_context
from dj_session import DJManager

try:
    from flask_cors import CORS
    _CORS_AVAILABLE = True
except ImportError:
    _CORS_AVAILABLE = False

# ── Config ──────────────────────────────────────────────────────────────────

PICKLE_PATH  = os.environ.get('DEEJAY_PICKLE_PATH',  r'C:\Dev\DeeJAI\pickles\mp3tovecs\mp3tovecs.p')
DB_PATH      = os.environ.get('DEEJAY_DB_PATH',      r'C:\Dev\DeeJAI\radio\plays.db')
LIBRARY_ROOT = os.environ.get('DEEJAY_LIBRARY_ROOT', r'E:\Media\Music')
API_HOST     = os.environ.get('DEEJAY_HOST',         '0.0.0.0')
API_PORT     = int(os.environ.get('DEEJAY_PORT',     '8050'))

RECENT_RING_SIZE = 50      # tracks excluded from recommendations
AUDIO_POOL_SIZE  = 50      # cosine-sim candidates before re-ranking
DEFAULT_N        = 10      # tracks returned per recommend call
DEFAULT_EPSILON  = 0.12    # exploration probability

# ── App ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)
if _CORS_AVAILABLE:
    CORS(app)

# ── Auth ─────────────────────────────────────────────────────────────────────

# Set DEEJAY_API_KEY env var to enable Bearer auth.
# If unset, auth is disabled (safe for LAN-only use).
_API_KEY: str | None = os.environ.get('DEEJAY_API_KEY') or None

_AUTH_EXEMPT = {'/api/health', '/admin',
                '/api/admin/stats', '/api/admin/config', '/api/admin/action'}

@app.before_request
def _check_auth():
    if _API_KEY is None:
        return  # auth disabled
    if request.path in _AUTH_EXEMPT:
        return  # open endpoints
    if request.path.startswith('/admin'):
        return  # admin UI always local
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer ') or auth[7:] != _API_KEY:
        return jsonify({'error': 'unauthorized'}), 401

# ── Global state ─────────────────────────────────────────────────────────────

_all_paths:   list              = []
_path_index:  dict              = {}   # path → int index (fast lookup)
_vecs:        np.ndarray | None = None
_track_meta:  dict              = {}   # abs_path → {artist, title, album}
_tag_index:   dict              = {}   # (norm_artist, norm_title) → abs_path
_scorer:      Scorer | None     = None
_recent_ring: deque             = deque(maxlen=RECENT_RING_SIZE)
_dj:          DJManager | None  = None
_ready:       bool              = False

# ── Helpers ──────────────────────────────────────────────────────────────────

_PUNCT = re.compile(r"[^\w\s]")
_WS    = re.compile(r"\s+")

def _normalize(s: str) -> str:
    s = (s or '').lower().strip()
    s = _PUNCT.sub('', s)
    return _WS.sub(' ', s).strip()


def _to_relative(abs_path: str) -> str:
    """Convert absolute Windows path to library-relative forward-slash path."""
    rel = abs_path
    if rel.lower().startswith(LIBRARY_ROOT.lower()):
        rel = rel[len(LIBRARY_ROOT):]
    return rel.replace('\\', '/').lstrip('/')


_TRACK_NUM_RE = re.compile(r'^\d+\s*[-–.]\s*')   # strip "01 - " from filenames

def _path_meta(path: str) -> dict:
    """Derive artist/title/album from directory structure when tags unavailable."""
    parts  = path.replace('\\', '/').split('/')
    artist = parts[-3] if len(parts) >= 3 else ''
    album  = parts[-2] if len(parts) >= 2 else ''
    fname  = os.path.splitext(parts[-1])[0] if parts else ''
    title  = _TRACK_NUM_RE.sub('', fname).strip()
    return {'artist': artist, 'title': title, 'album': album}


def _load_tags(paths: list) -> tuple:
    """
    Build tag metadata for all paths.
    Priority:
      1. track_meta.json  (pre-cached, instant)
      2. Path-based derivation  (fast fallback for uncached tracks)
    Mutagen is NOT used at startup — too slow for 22k files.
    """
    meta  = {}
    index = {}

    # Load pre-cached metadata
    cached = {}
    meta_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'track_meta.json')
    if os.path.exists(meta_path):
        import json as _json
        with open(meta_path, encoding='utf-8') as f:
            cached = _json.load(f)
        print(f"[startup] Loaded {len(cached)} cached tag entries")

    hit = miss = 0
    for path in paths:
        if path in cached:
            entry  = cached[path]
            artist = entry.get('artist', '')
            title  = entry.get('title',  '')
            album  = entry.get('album',  '')
            hit   += 1
        else:
            derived = _path_meta(path)
            artist  = derived['artist']
            title   = derived['title']
            album   = derived['album']
            miss   += 1

        meta[path] = {'artist': artist, 'title': title, 'album': album}
        key = (_normalize(artist), _normalize(title))
        if key not in index:
            index[key] = path

    print(f"[startup] Tags: {hit} cached + {miss} path-derived = {hit+miss} total")
    return meta, index


def _load_pickle() -> tuple[list, np.ndarray] | None:
    """Load and validate the embeddings pickle. Returns (paths, vecs) or None on failure."""
    if not os.path.exists(PICKLE_PATH):
        print(f"[pickle] ERROR: file not found: {PICKLE_PATH}")
        return None
    try:
        with open(PICKLE_PATH, 'rb') as f:
            data = pickle.load(f)
    except Exception as e:
        print(f"[pickle] ERROR: failed to load: {e}")
        return None

    if not isinstance(data, dict) or len(data) == 0:
        print(f"[pickle] ERROR: expected non-empty dict, got {type(data).__name__} with {len(data) if hasattr(data, '__len__') else '?'} entries")
        return None

    paths = list(data.keys())
    values = list(data.values())

    # Validate vector dimensions are consistent
    try:
        raw = np.array(values, dtype=np.float32)
    except (ValueError, TypeError) as e:
        print(f"[pickle] ERROR: vectors have inconsistent shapes: {e}")
        return None

    if raw.ndim != 2:
        print(f"[pickle] ERROR: expected 2D array, got {raw.ndim}D")
        return None

    print(f"[pickle] OK: {len(paths)} tracks, {raw.shape[1]}D vectors")
    return paths, raw


def _startup():
    global _all_paths, _path_index, _vecs, _track_meta, _tag_index, _scorer, _dj, _ready

    print("[startup] Loading embeddings…")
    result = _load_pickle()
    if result is None:
        print("[startup] FATAL: could not load embeddings pickle — API will not start")
        return

    paths, raw = result

    # Pre-normalise for cosine similarity via dot product
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs  = raw / norms

    _all_paths  = paths
    _path_index = {p: i for i, p in enumerate(paths)}
    _vecs       = vecs
    print(f"[startup] {len(paths)} tracks loaded")

    _track_meta, _tag_index = _load_tags(paths)
    print("[startup] Tags indexed")

    _scorer = Scorer(db_path=DB_PATH)
    print(f"[startup] Scorer ready — {_scorer.stats_summary()}")

    _dj = DJManager(
        vecs=_vecs, path_index=_path_index, all_paths=_all_paths,
        scorer=_scorer, track_meta=_track_meta, recent_ring=_recent_ring,
        find_seed=_find_seed, build_payload=_build_track_payload,
        log_play=_log_play, explore_cands=_explore_candidates,
    )
    print("[startup] DJ manager ready")

    _ready = True
    print(f"[startup] API live on {API_HOST}:{API_PORT}")


def _find_seed(artist: str, title: str):
    """Return abs_path matching artist+title, or None."""
    key = (_normalize(artist), _normalize(title))
    if key in _tag_index:
        return _tag_index[key]
    # Relax: title-only match (handles 'feat.' variants etc.)
    norm_t = _normalize(title)
    for (a, t), path in _tag_index.items():
        if t == norm_t:
            return path
    return None


def _cosine_candidates(seed_path: str) -> list:
    """Top-AUDIO_POOL_SIZE tracks by cosine similarity, excluding ring buffer."""
    idx      = _path_index[seed_path]
    sims     = np.dot(_vecs, _vecs[idx])
    pool     = min(AUDIO_POOL_SIZE + RECENT_RING_SIZE + 5, len(_all_paths))
    top_idxs = np.argpartition(sims, -pool)[-pool:]
    result   = [
        (_all_paths[i], float(sims[i]))
        for i in top_idxs
        if _all_paths[i] != seed_path and _all_paths[i] not in _recent_ring
    ]
    result.sort(key=lambda x: -x[1])
    return result[:AUDIO_POOL_SIZE]


def _explore_candidates() -> list:
    """Random candidates weighted toward underplayed tracks."""
    try:
        con    = sqlite3.connect(DB_PATH)
        played = {r[0]: r[1] for r in con.execute(
            "SELECT track_path, COUNT(*) FROM plays GROUP BY track_path"
        ).fetchall()}
        con.close()
    except Exception:
        played = {}

    weights = np.array([1.0 / (1 + played.get(p, 0)) for p in _all_paths],
                       dtype=np.float64)
    weights /= weights.sum()
    chosen  = np.random.choice(len(_all_paths),
                                size=min(AUDIO_POOL_SIZE * 2, len(_all_paths)),
                                replace=False, p=weights)
    return [
        (_all_paths[i], 0.5)
        for i in chosen
        if _all_paths[i] not in _recent_ring
    ][:AUDIO_POOL_SIZE]


def _rescore(candidates: list, context: str) -> list:
    """Score candidates with scorer, return sorted (path, score) list."""
    scored = [
        (path, _scorer.score_candidate(path, sim, _track_meta, context))
        for path, sim in candidates
    ]
    scored.sort(key=lambda x: -x[1])
    return scored


def _build_track_payload(path: str, score: float) -> dict:
    meta = _track_meta.get(path, {})
    return {
        'artist':        meta.get('artist', ''),
        'title':         meta.get('title',  ''),
        'album':         meta.get('album',  ''),
        'relative_path': _to_relative(path),
        'score':         round(score, 4),
    }


def _log_play(artist: str, title: str, album: str, ms_played: int,
              completed: int, context: str | None = None,
              track_path: str | None = None) -> str:
    """Write a play/skip event to plays.db, append to the recent ring, and bump
    the scorer's refresh counter. Returns the resolved track_path.
    Shared by /api/played and /api/dj/next. Raises on DB error."""
    context = context or get_context()
    if track_path is None:
        track_path = _find_seed(artist, title) or f'unknown:{_normalize(artist)}:{_normalize(title)}'
    started_at = time.time() - ms_played / 1000.0
    con = sqlite3.connect(DB_PATH)
    con.execute(
        'INSERT OR IGNORE INTO plays '
        '(track_path, artist, title, album, started_at, ms_played, completed, context) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (track_path, artist, title, album, started_at, ms_played, completed, context)
    )
    con.commit()
    con.close()
    _recent_ring.append(track_path)
    _scorer.on_play_logged()
    return track_path


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/api/health')
def health():
    return jsonify({'ok': _ready, 'tracks': len(_all_paths)})


@app.route('/api/recommend')
def recommend():
    if not _ready:
        return jsonify({'error': 'server not ready'}), 503

    artist  = request.args.get('artist',  '').strip()
    title   = request.args.get('title',   '').strip()
    n       = min(int(request.args.get('n',       DEFAULT_N)),      50)
    epsilon = float(request.args.get('epsilon',   DEFAULT_EPSILON))
    context = request.args.get('context', '').strip() or get_context()

    if not artist and not title:
        return jsonify({'error': 'artist or title required'}), 400

    seed_path = _find_seed(artist, title)
    if not seed_path:
        return jsonify({'error': f'not found: {artist} – {title}'}), 404

    seed_meta = _track_meta.get(seed_path, {})

    # Epsilon-greedy exploration
    exploring  = random.random() < epsilon
    candidates = _explore_candidates() if exploring else _cosine_candidates(seed_path)

    ranked = _rescore(candidates, context)[:n]
    tracks = [_build_track_payload(p, s) for p, s in ranked]

    return jsonify({
        'tracks':    tracks,
        'seed':      {'artist': seed_meta.get('artist'), 'title': seed_meta.get('title')},
        'context':   context,
        'exploring': exploring,
        'n':         len(tracks),
    })


@app.route('/api/played', methods=['POST'])
def played():
    if not _ready:
        return jsonify({'error': 'server not ready'}), 503

    body      = request.get_json(force=True) or {}
    artist    = body.get('artist',    '').strip()
    title     = body.get('title',     '').strip()
    album     = body.get('album',     '').strip()
    ms_played = int(body.get('ms_played',  0))
    completed = int(bool(body.get('completed', False)))
    context   = body.get('context',   '').strip() or None

    try:
        _log_play(artist, title, album, ms_played, completed, context)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True})


@app.route('/api/home')
def home():
    if not _ready:
        return jsonify({'error': 'server not ready'}), 503

    context = request.args.get('context', '').strip() or get_context()

    top_artists = [
        {'artist': a, 'score': round(s, 4)}
        for a, s in _scorer.top_artists(context, n=20)
    ]

    # Only surface tracks that have embeddings (i.e. are seedable)
    suggested = [
        {'artist': a, 'title': t, 'score': round(s, 4)}
        for a, t, s in _scorer.top_tracks(context, n=100)
        if _find_seed(a, t) is not None
    ][:20]

    try:
        con    = sqlite3.connect(DB_PATH)
        rows   = con.execute(
            'SELECT artist, title, album, completed, context '
            'FROM plays ORDER BY started_at DESC LIMIT 20'
        ).fetchall()
        con.close()
        recent = [
            {'artist': r[0], 'title': r[1], 'album': r[2],
             'completed': bool(r[3]), 'context': r[4]}
            for r in rows
        ]
    except Exception:
        recent = []

    return jsonify({
        'context':      context,
        'top_artists':  top_artists,
        'suggested':    suggested,
        'recent_plays': recent,
    })


# ── DJ session ────────────────────────────────────────────────────────────────

@app.route('/api/dj/start', methods=['POST'])
def dj_start():
    if not _ready:
        return jsonify({'error': 'server not ready'}), 503
    body = request.get_json(force=True, silent=True) or {}
    hour = body.get('hour', None)
    try:
        hour = float(hour) if hour is not None else None
    except (TypeError, ValueError):
        hour = None
    seed_artist = (body.get('seed_artist') or '').strip()
    seed_title  = (body.get('seed_title')  or '').strip()
    result = _dj.start(hour=hour, seed_artist=seed_artist, seed_title=seed_title)
    if result is None:
        return jsonify({'error': 'could not seed a session'}), 503
    return jsonify(result)


@app.route('/api/dj/next', methods=['POST'])
def dj_next():
    if not _ready:
        return jsonify({'error': 'server not ready'}), 503
    body = request.get_json(force=True, silent=True) or {}
    session_id = (body.get('session_id') or '').strip()
    if not session_id:
        return jsonify({'error': 'session_id required'}), 400
    completed = bool(body.get('completed', False))
    ms_played = int(body.get('ms_played', 0))
    result = _dj.next(session_id, completed, ms_played)
    if result is None:
        return jsonify({'error': 'session not found'}), 404
    return jsonify(result)


@app.route('/api/dj/session/<session_id>', methods=['GET', 'DELETE'])
def dj_session(session_id):
    if not _ready:
        return jsonify({'error': 'server not ready'}), 503
    if request.method == 'DELETE':
        ok = _dj.end(session_id)
        return (jsonify({'ok': True}), 200) if ok else (jsonify({'error': 'session not found'}), 404)
    result = _dj.inspect(session_id)
    if result is None:
        return jsonify({'error': 'session not found'}), 404
    return jsonify(result)


# ── Admin ────────────────────────────────────────────────────────────────────

# Tunable config — can be updated at runtime via POST /api/admin/config
_config = {
    'audio_w':               0.40,
    'beh_w':                 0.60,
    'epsilon':               0.12,
    'recency_half_life_days': 30,
    'min_plays_for_signal':   3,
    'recent_ring_size':       50,
    # AI DJ
    'drift_alpha':             0.25,
    'skip_streak_transition':  2,
    'chapter_max_tracks':      6,
    'target_far_z':           -1.0,
    'bridge_min_z':            0.0,
    'dj_epsilon':              0.08,
}


@app.route('/api/admin/stats')
def admin_stats():
    if not _ready:
        return jsonify({'error': 'server not ready'}), 503
    try:
        con = sqlite3.connect(DB_PATH)

        totals = con.execute(
            'SELECT COUNT(*), SUM(completed), COUNT(DISTINCT track_path) '
            'FROM plays'
        ).fetchone()
        total_plays, total_completions, unique_tracks = totals
        completion_pct = round(100.0 * total_completions / total_plays, 1) if total_plays else 0

        ctx_rows = con.execute(
            'SELECT context, COUNT(*), SUM(completed) FROM plays GROUP BY context'
        ).fetchall()
        context_split = [
            {'context': r[0] or 'unknown', 'plays': r[1],
             'completions': r[2],
             'pct': round(100.0 * r[2] / r[1], 1) if r[1] else 0}
            for r in ctx_rows
        ]

        top_artists = con.execute(
            'SELECT artist, COUNT(*) as plays, SUM(completed) as completions '
            'FROM plays WHERE artist != "" '
            'GROUP BY artist ORDER BY plays DESC LIMIT 25'
        ).fetchall()

        top_tracks = con.execute(
            'SELECT artist, title, COUNT(*) as plays, SUM(completed) as completions '
            'FROM plays WHERE artist != "" AND title != "" '
            'GROUP BY artist, title ORDER BY plays DESC LIMIT 25'
        ).fetchall()

        recent = con.execute(
            'SELECT artist, title, completed, context, '
            'datetime(started_at, "unixepoch", "localtime") as ts '
            'FROM plays ORDER BY started_at DESC LIMIT 30'
        ).fetchall()

        con.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({
        'summary': {
            'total_plays':      total_plays,
            'total_completions': total_completions,
            'completion_pct':   completion_pct,
            'unique_tracks':    unique_tracks,
            'embedded_tracks':  len(_all_paths),
        },
        'context_split': context_split,
        'top_artists': [
            {'artist': r[0], 'plays': r[1], 'completions': r[2],
             'pct': round(100.0 * r[2] / r[1], 1) if r[1] else 0}
            for r in top_artists
        ],
        'top_tracks': [
            {'artist': r[0], 'title': r[1], 'plays': r[2], 'completions': r[3],
             'pct': round(100.0 * r[3] / r[2], 1) if r[2] else 0}
            for r in top_tracks
        ],
        'recent': [
            {'artist': r[0], 'title': r[1], 'completed': bool(r[2]),
             'context': r[3], 'ts': r[4]}
            for r in recent
        ],
        'ring_buffer': list(_recent_ring),
        'config': _config,
    })


@app.route('/api/admin/config', methods=['GET', 'POST'])
def admin_config():
    global DEFAULT_EPSILON, RECENT_RING_SIZE
    if request.method == 'GET':
        return jsonify(_config)
    body = request.get_json(force=True) or {}
    numeric = {
        'audio_w', 'beh_w', 'epsilon',
        'recency_half_life_days', 'min_plays_for_signal', 'recent_ring_size',
        'drift_alpha', 'skip_streak_transition', 'chapter_max_tracks',
        'target_far_z', 'bridge_min_z', 'dj_epsilon',
    }
    for k, v in body.items():
        if k in numeric:
            _config[k] = float(v)
    # Apply live where possible
    DEFAULT_EPSILON  = _config['epsilon']
    RECENT_RING_SIZE = int(_config['recent_ring_size'])
    # Push scorer knobs
    if _scorer:
        import scorer as _scorer_mod
        _scorer_mod.AUDIO_W                  = _config['audio_w']
        _scorer_mod.BEH_W                    = _config['beh_w']
        _scorer_mod.RECENCY_HALF_LIFE_DAYS   = _config['recency_half_life_days']
        _scorer_mod.MIN_PLAYS_FOR_SIGNAL     = int(_config['min_plays_for_signal'])
    # Push DJ knobs (module globals are read at call time, so this is live)
    import dj_session as _dj_mod
    _dj_mod.DRIFT_ALPHA            = _config['drift_alpha']
    _dj_mod.SKIP_STREAK_TRANSITION = int(_config['skip_streak_transition'])
    _dj_mod.CHAPTER_MAX_TRACKS     = int(_config['chapter_max_tracks'])
    _dj_mod.TARGET_FAR_Z           = _config['target_far_z']
    _dj_mod.BRIDGE_MIN_Z           = _config['bridge_min_z']
    _dj_mod.DJ_EPSILON             = _config['dj_epsilon']
    return jsonify({'ok': True, 'config': _config})


@app.route('/api/admin/action', methods=['POST'])
def admin_action():
    body   = request.get_json(force=True) or {}
    action = body.get('action', '')
    if action == 'clear_ring':
        n = len(_recent_ring)
        _recent_ring.clear()
        return jsonify({'ok': True, 'message': f'✓ Cleared {n} tracks from ring buffer'})
    if action == 'refresh_scorer':
        if _scorer:
            _scorer.refresh()
            return jsonify({'ok': True, 'message': f'✓ Scorer refreshed'})
        return jsonify({'ok': False, 'message': 'Scorer not ready'}), 503
    if action == 'reload_embeddings':
        global _all_paths, _path_index, _vecs, _track_meta, _tag_index, _dj, _ready
        result = _load_pickle()
        if result is None:
            return jsonify({'ok': False, 'message': '✗ Pickle reload failed — check server logs'}), 500
        paths, raw = result
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vecs = raw / norms
        _all_paths  = paths
        _path_index = {p: i for i, p in enumerate(paths)}
        _vecs       = vecs
        _track_meta, _tag_index = _load_tags(paths)
        if _scorer:
            _scorer.refresh()
        # Rebuild DJ manager with new vecs
        if _dj:
            _dj = DJManager(
                vecs=_vecs, path_index=_path_index, all_paths=_all_paths,
                scorer=_scorer, track_meta=_track_meta, recent_ring=_recent_ring,
                find_seed=_find_seed, build_payload=_build_track_payload,
                log_play=_log_play, explore_cands=_explore_candidates,
            )
        _ready = True
        return jsonify({'ok': True, 'message': f'✓ Reloaded {len(paths)} tracks'})
    return jsonify({'ok': False, 'message': f'Unknown action: {action}'}), 400


NAVIDROME_URL = os.environ.get('DEEJAY_NAVIDROME_URL', 'http://192.168.0.29:4533')
INBOX_DIR     = os.environ.get('DEEJAY_INBOX_DIR',  r'E:\Media\MusicInbox')
PENDING_DIR   = os.environ.get('DEEJAY_PENDING_DIR', r'E:\Media\MusicPendingReview')

@app.route('/api/health/all')
def health_all():
    """Aggregate health check for the entire ecosystem."""
    import urllib.request
    import datetime

    checks = {}

    # 1. API / Pickle
    pickle_mtime = None
    pickle_age_hours = None
    try:
        if os.path.exists(PICKLE_PATH):
            mtime = os.path.getmtime(PICKLE_PATH)
            pickle_mtime = datetime.datetime.fromtimestamp(mtime).isoformat()
            pickle_age_hours = round((time.time() - mtime) / 3600, 1)
    except Exception:
        pass

    checks['api'] = {
        'status': 'ok' if _ready else 'error',
        'tracks': len(_all_paths),
        'pickle_mtime': pickle_mtime,
        'pickle_age_hours': pickle_age_hours,
    }

    # 2. Navidrome
    try:
        req = urllib.request.Request(f'{NAVIDROME_URL}/api/ping', method='GET')
        with urllib.request.urlopen(req, timeout=5) as resp:
            nav_data = json.loads(resp.read())
            checks['navidrome'] = {
                'status': 'ok' if nav_data.get('status') == 'ok' else 'error',
                'url': NAVIDROME_URL,
            }
    except Exception as e:
        checks['navidrome'] = {'status': 'error', 'url': NAVIDROME_URL, 'error': str(e)}

    # 3. plays.db
    try:
        if os.path.exists(DB_PATH):
            db_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(DB_PATH)).isoformat()
            con = sqlite3.connect(DB_PATH)
            row = con.execute('SELECT COUNT(*), MAX(started_at) FROM plays').fetchone()
            con.close()
            checks['plays_db'] = {
                'status': 'ok',
                'total_plays': row[0],
                'last_play_ts': datetime.datetime.fromtimestamp(row[1]).isoformat() if row[1] else None,
                'last_modified': db_mtime,
            }
        else:
            checks['plays_db'] = {'status': 'error', 'error': 'file not found'}
    except Exception as e:
        checks['plays_db'] = {'status': 'error', 'error': str(e)}

    # 4. Inbox (new music waiting)
    try:
        if os.path.isdir(INBOX_DIR):
            inbox_files = [f for f in os.listdir(INBOX_DIR) if os.path.isfile(os.path.join(INBOX_DIR, f))]
            checks['inbox'] = {'status': 'ok', 'pending': len(inbox_files)}
        else:
            checks['inbox'] = {'status': 'ok', 'pending': 0, 'note': 'directory not found'}
    except Exception as e:
        checks['inbox'] = {'status': 'error', 'error': str(e)}

    # 5. Pending review
    try:
        if os.path.isdir(PENDING_DIR):
            pending_files = [f for f in os.listdir(PENDING_DIR) if os.path.isfile(os.path.join(PENDING_DIR, f))]
            checks['pending_review'] = {'status': 'ok', 'count': len(pending_files)}
        else:
            checks['pending_review'] = {'status': 'ok', 'count': 0, 'note': 'directory not found'}
    except Exception as e:
        checks['pending_review'] = {'status': 'error', 'error': str(e)}

    # Overall status
    all_ok = all(c.get('status') == 'ok' for c in checks.values())
    return jsonify({'overall': 'ok' if all_ok else 'degraded', 'checks': checks})


@app.route('/admin/health')
def admin_health_ui():
    return _HEALTH_HTML


_HEALTH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeejAI · System Health</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3e;
    --accent: #6c8ef7; --green: #4caf82; --red: #e05c5c; --yellow: #d4a843;
    --text: #e2e4ee; --muted: #7b7f96;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }
  header { background: var(--surface); border-bottom: 1px solid var(--border);
           padding: 14px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 17px; font-weight: 600; color: var(--accent); }
  header .nav { margin-left: auto; display: flex; gap: 12px; }
  header .nav a { color: var(--muted); text-decoration: none; font-size: 13px; }
  header .nav a:hover { color: var(--accent); }
  #overall { display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; }
  #overall.ok { background: rgba(76,175,130,0.2); color: var(--green); }
  #overall.degraded { background: rgba(212,168,67,0.2); color: var(--yellow); }
  #overall.error { background: rgba(224,92,92,0.2); color: var(--red); }
  main { padding: 24px; max-width: 900px; display: grid; gap: 12px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
          padding: 18px 20px; display: grid; grid-template-columns: 100px 1fr auto; align-items: center; gap: 16px; }
  .card .label { font-size: 13px; font-weight: 600; }
  .card .details { font-size: 12px; color: var(--muted); }
  .card .details span { margin-right: 16px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; }
  .dot.ok { background: var(--green); }
  .dot.error { background: var(--red); }
  .dot.warn { background: var(--yellow); }
  .footer { margin-top: 24px; font-size: 11px; color: var(--muted); text-align: center; }
</style>
</head>
<body>
<header>
  <h1>System Health</h1>
  <span id="overall">loading…</span>
  <div class="nav">
    <a href="/admin">Dashboard</a>
    <a href="/api/health/all">Raw JSON</a>
  </div>
</header>
<main id="cards"></main>
<div class="footer">Auto-refreshes every 30s</div>
<script>
const LABELS = {
  api:           'DeejAI API',
  navidrome:     'Navidrome',
  plays_db:      'Plays DB',
  inbox:         'Inbox',
  pending_review:'Pending Review',
};

function fmt(key, c) {
  const parts = [];
  if (c.tracks !== undefined)        parts.push(`<span>${c.tracks.toLocaleString()} tracks</span>`);
  if (c.pickle_age_hours !== null)   parts.push(`<span>pickle: ${c.pickle_age_hours}h old</span>`);
  if (c.total_plays !== undefined)   parts.push(`<span>${c.total_plays.toLocaleString()} plays</span>`);
  if (c.last_play_ts)                parts.push(`<span>last: ${c.last_play_ts.split('T')[0]}</span>`);
  if (c.pending !== undefined)       parts.push(`<span>${c.pending} files</span>`);
  if (c.count !== undefined)         parts.push(`<span>${c.count} tracks</span>`);
  if (c.url)                         parts.push(`<span>${c.url}</span>`);
  if (c.error)                       parts.push(`<span style="color:var(--red)">${c.error}</span>`);
  return parts.join('');
}

async function load() {
  try {
    const r = await fetch('/api/health/all');
    const d = await r.json();
    const ov = document.getElementById('overall');
    ov.textContent = d.overall;
    ov.className = d.overall;

    const cards = document.getElementById('cards');
    cards.innerHTML = Object.entries(d.checks).map(([k, c]) => `
      <div class="card">
        <div class="label">${LABELS[k] || k}</div>
        <div class="details">${fmt(k, c)}</div>
        <div class="dot ${c.status}"></div>
      </div>
    `).join('');
  } catch(e) {
    document.getElementById('overall').textContent = 'error';
    document.getElementById('overall').className = 'error';
  }
}
load();
setInterval(load, 30000);
</script>
</body>
</html>"""


# ── Review Dashboard ────────────────────────────────────────────────────────

@app.route('/api/admin/review')
def review_list():
    """List pending tracks awaiting review."""
    if not os.path.isdir(PENDING_DIR):
        return jsonify({'pending': [], 'count': 0})

    files = []
    for f in sorted(os.listdir(PENDING_DIR)):
        fp = os.path.join(PENDING_DIR, f)
        if not os.path.isfile(fp):
            continue
        stat = os.stat(fp)
        # Derive metadata from path
        meta = _path_meta(f)
        files.append({
            'filename': f,
            'size_mb': round(stat.st_size / 1048576, 1),
            'added': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)),
            'artist': meta['artist'],
            'title': meta['title'],
            'album': meta['album'],
        })

    return jsonify({'pending': files, 'count': len(files)})


@app.route('/api/admin/review/confirm', methods=['POST'])
def review_confirm():
    """Confirm a pending track — move to library and trigger reload."""
    body = request.get_json(force=True) or {}
    filename = body.get('filename', '').strip()
    if not filename:
        return jsonify({'error': 'filename required'}), 400

    src = os.path.join(PENDING_DIR, filename)
    if not os.path.isfile(src):
        return jsonify({'error': f'not found: {filename}'}), 404

    # Derive destination from metadata
    meta = _path_meta(filename)
    artist_dir = os.path.join(LIBRARY_ROOT, meta['artist']) if meta['artist'] else LIBRARY_ROOT
    album_dir = os.path.join(artist_dir, meta['album']) if meta['album'] else artist_dir
    os.makedirs(album_dir, exist_ok=True)

    dest = os.path.join(album_dir, filename)
    if os.path.exists(dest):
        return jsonify({'error': f'already exists: {dest}'}), 409

    shutil.move(src, dest)

    # Add to track_meta.json
    abs_path = dest
    _track_meta[abs_path] = {'artist': meta['artist'], 'title': meta['title'], 'album': meta['album']}
    key = (_normalize(meta['artist']), _normalize(meta['title']))
    if key not in _tag_index:
        _tag_index[key] = abs_path

    # Save updated track_meta.json
    try:
        meta_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'track_meta.json')
        import json as _json
        with open(meta_path, 'w', encoding='utf-8') as f:
            _json.dump(_track_meta, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"[review] warning: failed to update track_meta.json: {e}")

    return jsonify({'ok': True, 'message': f'✓ Confirmed: {filename}', 'dest': dest})


@app.route('/api/admin/review/reject', methods=['POST'])
def review_reject():
    """Reject a pending track — delete and log."""
    body = request.get_json(force=True) or {}
    filename = body.get('filename', '').strip()
    reason = body.get('reason', '').strip()
    if not filename:
        return jsonify({'error': 'filename required'}), 400

    src = os.path.join(PENDING_DIR, filename)
    if not os.path.isfile(src):
        return jsonify({'error': f'not found: {filename}'}), 404

    os.remove(src)

    # Log rejection
    try:
        log_path = os.path.join(PENDING_DIR, 'rejection_log.jsonl')
        import json as _json
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(_json.dumps({
                'filename': filename,
                'reason': reason,
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
            }) + '\n')
    except Exception:
        pass

    return jsonify({'ok': True, 'message': f'✓ Rejected: {filename}'})


@app.route('/admin/review')
def review_ui():
    return _REVIEW_HTML


_REVIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeejAI · Review Queue</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3e;
    --accent: #6c8ef7; --green: #4caf82; --red: #e05c5c; --yellow: #d4a843;
    --text: #e2e4ee; --muted: #7b7f96;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }
  header { background: var(--surface); border-bottom: 1px solid var(--border);
           padding: 14px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 17px; font-weight: 600; color: var(--accent); }
  header .nav { margin-left: auto; display: flex; gap: 12px; }
  header .nav a { color: var(--muted); text-decoration: none; font-size: 13px; }
  header .nav a:hover { color: var(--accent); }
  main { padding: 24px; max-width: 1000px; }
  .count { font-size: 13px; color: var(--muted); margin-bottom: 16px; }
  .empty { color: var(--muted); font-style: italic; padding: 40px; text-align: center; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 11px; color: var(--muted); text-transform: uppercase;
       letter-spacing: .05em; padding: 8px 12px; border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:hover { background: rgba(108,142,247,0.05); }
  .btn { border: none; padding: 5px 12px; border-radius: 5px; cursor: pointer;
         font-size: 12px; font-weight: 600; margin-right: 6px; }
  .btn-confirm { background: rgba(76,175,130,0.2); color: var(--green); }
  .btn-reject { background: rgba(224,92,92,0.2); color: var(--red); }
  .btn:hover { opacity: 0.8; }
  #msg { font-size: 12px; margin-top: 12px; min-height: 20px; }
</style>
</head>
<body>
<header>
  <h1>Review Queue</h1>
  <div class="nav">
    <a href="/admin">Dashboard</a>
    <a href="/admin/health">Health</a>
  </div>
</header>
<main>
  <div class="count" id="count"></div>
  <div id="list"></div>
  <div id="msg"></div>
</main>
<script>
async function load() {
  const r = await fetch('/api/admin/review');
  const d = await r.json();
  document.getElementById('count').textContent = d.count + ' tracks pending review';
  const list = document.getElementById('list');
  if (d.count === 0) { list.innerHTML = '<div class="empty">No tracks pending review</div>'; return; }
  list.innerHTML = '<table><tr><th>File</th><th>Artist</th><th>Title</th><th>Size</th><th>Added</th><th>Actions</th></tr>'
    + d.pending.map(f => `<tr>
      <td>${esc(f.filename)}</td><td>${esc(f.artist)}</td><td>${esc(f.title)}</td>
      <td>${f.size_mb} MB</td><td>${f.added}</td>
      <td>
        <button class="btn btn-confirm" onclick="doAction('confirm','${esc(f.filename)}')">Confirm</button>
        <button class="btn btn-reject" onclick="doAction('reject','${esc(f.filename)}')">Reject</button>
      </td>
    </tr>`).join('') + '</table>';
}
function esc(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
async function doAction(action, filename) {
  const msg = document.getElementById('msg');
  msg.textContent = 'working…';
  const r = await fetch('/api/admin/review/' + action, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({filename})
  });
  const d = await r.json();
  msg.textContent = d.message || d.error;
  msg.style.color = d.ok ? 'var(--green)' : 'var(--red)';
  load();
}
load();
</script>
</body>
</html>"""


# ── Genre Dashboard ─────────────────────────────────────────────────────────

@app.route('/api/admin/genres')
def genre_stats():
    """Genre distribution from track_meta.json."""
    genre_counts = {}
    for meta in _track_meta.values():
        genre = (meta.get('genre') or 'Unknown').strip() or 'Unknown'
        genre_counts[genre] = genre_counts.get(genre, 0) + 1

    # Sort by count descending
    sorted_genres = sorted(genre_counts.items(), key=lambda x: -x[1])
    total = sum(genre_counts.values())

    return jsonify({
        'genres': [{'name': g, 'count': c, 'pct': round(100 * c / total, 1) if total else 0}
                   for g, c in sorted_genres],
        'total': total,
        'unique': len(genre_counts),
    })


@app.route('/api/admin/genres/normalize', methods=['POST'])
def genre_normalize():
    """Apply genre normalization using the pipeline's genre_normalizer."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'import_pipeline'))
        from genre_normalizer import normalize_genre, load_mappings

        mappings = load_mappings()
        changed = 0
        for path, meta in _track_meta.items():
            old_genre = (meta.get('genre') or '').strip()
            if not old_genre:
                continue
            new_genre = normalize_genre(old_genre, mappings)
            if new_genre != old_genre:
                meta['genre'] = new_genre
                changed += 1

        if changed > 0:
            meta_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'track_meta.json')
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(_track_meta, f, ensure_ascii=False, indent=1)

        return jsonify({'ok': True, 'message': f'✓ Normalized {changed} tracks', 'changed': changed})
    except ImportError as e:
        return jsonify({'ok': False, 'error': f'genre_normalizer not available: {e}'}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/admin/genres')
def genre_ui():
    return _GENRE_HTML


_GENRE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeejAI · Genre Distribution</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3e;
    --accent: #6c8ef7; --green: #4caf82; --red: #e05c5c; --yellow: #d4a843;
    --text: #e2e4ee; --muted: #7b7f96;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }
  header { background: var(--surface); border-bottom: 1px solid var(--border);
           padding: 14px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 17px; font-weight: 600; color: var(--accent); }
  header .nav { margin-left: auto; display: flex; gap: 12px; }
  header .nav a { color: var(--muted); text-decoration: none; font-size: 13px; }
  header .nav a:hover { color: var(--accent); }
  main { padding: 24px; max-width: 900px; }
  .summary { display: flex; gap: 24px; margin-bottom: 20px; font-size: 13px; color: var(--muted); }
  .summary b { color: var(--accent); }
  .bar-row { display: grid; grid-template-columns: 160px 1fr 60px; align-items: center; gap: 12px;
             padding: 6px 0; border-bottom: 1px solid var(--border); }
  .bar-row .name { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar-bg { height: 18px; background: var(--surface); border-radius: 4px; overflow: hidden; }
  .bar-fill { height: 100%; background: var(--accent); border-radius: 4px; transition: width 0.5s; }
  .bar-row .count { font-size: 12px; color: var(--muted); text-align: right; }
  .btn { border: none; padding: 8px 18px; border-radius: 6px; cursor: pointer;
         font-size: 13px; font-weight: 600; margin-top: 16px; }
  .btn-normalize { background: var(--accent); color: #fff; }
  .btn:hover { opacity: .85; }
  #msg { font-size: 12px; margin-top: 12px; min-height: 20px; }
</style>
</head>
<body>
<header>
  <h1>Genre Distribution</h1>
  <div class="nav">
    <a href="/admin">Dashboard</a>
    <a href="/admin/health">Health</a>
  </div>
</header>
<main>
  <div class="summary" id="summary"></div>
  <div id="bars"></div>
  <button class="btn btn-normalize" onclick="normalize()">Normalize Genres</button>
  <div id="msg"></div>
</main>
<script>
async function load() {
  const r = await fetch('/api/admin/genres');
  const d = await r.json();
  document.getElementById('summary').innerHTML =
    `<span><b>${d.total.toLocaleString()}</b> tracks</span><span><b>${d.unique}</b> unique genres</span>`;
  const max = d.genres[0]?.count || 1;
  document.getElementById('bars').innerHTML = d.genres.map(g => `
    <div class="bar-row">
      <div class="name" title="${g.name}">${g.name}</div>
      <div class="bar-bg"><div class="bar-fill" style="width:${(g.count/max*100).toFixed(1)}%"></div></div>
      <div class="count">${g.count} (${g.pct}%)</div>
    </div>
  `).join('');
}
async function normalize() {
  const msg = document.getElementById('msg');
  msg.textContent = 'normalizing…';
  const r = await fetch('/api/admin/genres/normalize', {method:'POST'});
  const d = await r.json();
  msg.textContent = d.message || d.error;
  msg.style.color = d.ok ? 'var(--green)' : 'var(--red)';
  if (d.ok) load();
}
load();
</script>
</body>
</html>"""


@app.route('/admin')
def admin_ui():
    return _ADMIN_HTML


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeejAI · Backend</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3e;
    --accent: #6c8ef7; --green: #4caf82; --red: #e05c5c;
    --text: #e2e4ee; --muted: #7b7f96;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }
  header { background: var(--surface); border-bottom: 1px solid var(--border);
           padding: 14px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 17px; font-weight: 600; color: var(--accent); }
  #status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); flex-shrink: 0; }
  #status-dot.err { background: var(--red); }
  #status-text { font-size: 12px; color: var(--muted); }
  #refresh-btn { margin-left: auto; background: transparent; border: 1px solid var(--border);
                 color: var(--muted); padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  #refresh-btn:hover { border-color: var(--accent); color: var(--accent); }
  main { padding: 24px; max-width: 900px; display: grid; gap: 20px; }
  .panel { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  .panel-header { padding: 12px 16px; font-size: 11px; font-weight: 600; border-bottom: 1px solid var(--border);
                  color: var(--muted); text-transform: uppercase; letter-spacing: .06em;
                  display: flex; align-items: center; gap: 10px; }
  .panel-header .hint { font-weight: 400; text-transform: none; letter-spacing: 0; margin-left: auto; }
  .stat-row { display: grid; grid-template-columns: repeat(4, 1fr); }
  .stat { padding: 16px; border-right: 1px solid var(--border); }
  .stat:last-child { border-right: none; }
  .stat .lbl { font-size: 11px; color: var(--muted); }
  .stat .val { font-size: 22px; font-weight: 700; color: var(--accent); margin-top: 4px; }
  .stat .sub { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .config-body { padding: 16px; display: grid; gap: 16px; }
  .cfg-row { display: grid; grid-template-columns: 190px 1fr 56px; align-items: center; gap: 12px; }
  .cfg-row label { font-size: 13px; }
  .cfg-row .desc { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .cfg-row input[type=range] { width: 100%; accent-color: var(--accent); }
  .cfg-row .num { font-size: 14px; font-weight: 700; color: var(--accent); text-align: right; }
  .cfg-divider { height: 1px; background: var(--border); margin: 4px 0; }
  .btn-row { padding: 0 16px 16px; display: flex; align-items: center; gap: 12px; }
  .btn { border: none; padding: 8px 18px; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600; }
  .btn-save  { background: var(--accent); color: #fff; }
  .btn-reset { background: transparent; border: 1px solid var(--border); color: var(--muted); }
  .btn:hover { opacity: .85; }
  #save-msg { font-size: 12px; color: var(--green); }
  .ring-body { padding: 12px 16px; }
  .ring-tags { display: flex; flex-wrap: wrap; gap: 6px; min-height: 32px; }
  .ring-tag { background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
              padding: 3px 8px; font-size: 11px; color: var(--muted); }
  .ring-empty { font-size: 12px; color: var(--muted); font-style: italic; }
  .action-body { padding: 16px; display: grid; gap: 12px; }
  .action-row { display: flex; align-items: center; gap: 14px; }
  .action-row .action-desc { font-size: 12px; color: var(--muted); flex: 1; }
  .btn-action { background: transparent; border: 1px solid var(--border); color: var(--text);
                padding: 7px 16px; border-radius: 6px; cursor: pointer; font-size: 12px; white-space: nowrap; }
  .btn-action:hover { border-color: var(--accent); color: var(--accent); }
  .btn-action.danger:hover { border-color: var(--red); color: var(--red); }
  #action-log { margin-top: 8px; font-size: 11px; color: var(--green); min-height: 16px; }
</style>
</head>
<body>
<header>
  <div id="status-dot"></div>
  <h1>DeejAI</h1>
  <span id="status-text">connecting…</span>
  <button id="refresh-btn" onclick="load()">↺ Refresh</button>
</header>
<main>

  <!-- Server status -->
  <div class="panel">
    <div class="panel-header">Server Status</div>
    <div class="stat-row" id="stat-row">
      <div class="stat"><div class="lbl">Status</div><div class="val" id="s-status">—</div></div>
      <div class="stat"><div class="lbl">Tracks loaded</div><div class="val" id="s-tracks">—</div><div class="sub">with embeddings</div></div>
      <div class="stat"><div class="lbl">Context</div><div class="val" id="s-ctx">—</div><div class="sub">current</div></div>
      <div class="stat"><div class="lbl">Ring buffer</div><div class="val" id="s-ring">—</div><div class="sub">tracks excluded</div></div>
    </div>
  </div>

  <!-- Scoring weights -->
  <div class="panel">
    <div class="panel-header">Scoring Weights <span class="hint">must sum to 1.0</span></div>
    <div class="config-body" id="cfg-weights"></div>
  </div>

  <!-- Recommendation tuning -->
  <div class="panel">
    <div class="panel-header">Recommendation Tuning</div>
    <div class="config-body" id="cfg-rec"></div>
  </div>

  <!-- Behavioral model -->
  <div class="panel">
    <div class="panel-header">Behavioral Model</div>
    <div class="config-body" id="cfg-beh"></div>
  </div>

  <!-- AI DJ -->
  <div class="panel">
    <div class="panel-header">AI DJ <span class="hint">thresholds are z-scores vs library similarity</span></div>
    <div class="config-body" id="cfg-dj"></div>
    <div class="btn-row">
      <button class="btn btn-save" onclick="saveConfig()">Save &amp; Apply</button>
      <button class="btn btn-reset" onclick="resetConfig()">Reset to defaults</button>
      <span id="save-msg"></span>
    </div>
  </div>

  <!-- Actions -->
  <div class="panel">
    <div class="panel-header">Actions</div>
    <div class="action-body">
      <div class="action-row">
        <button class="btn-action" onclick="doAction('clear_ring')">Clear ring buffer</button>
        <span class="action-desc">Remove all recently-played tracks from the exclusion window so they can be recommended again immediately.</span>
      </div>
      <div class="action-row">
        <button class="btn-action" onclick="doAction('refresh_scorer')">Refresh scorer cache</button>
        <span class="action-desc">Force re-read of plays.db into the in-memory behavioral model without restarting the server.</span>
      </div>
      <div class="action-row">
        <button class="btn-action" onclick="doAction('reload_embeddings')">Reload embeddings</button>
        <span class="action-desc">Re-read the pickle file to pick new tracks without restarting the container. Validates before applying.</span>
      </div>
      <div id="action-log"></div>
    </div>
  </div>

  <!-- Ring buffer viewer -->
  <div class="panel">
    <div class="panel-header">Ring Buffer <span id="ring-label" class="hint"></span></div>
    <div class="ring-body"><div class="ring-tags" id="ring-tags"></div></div>
  </div>

</main>
<script>
const DEFAULTS = {
  audio_w: 0.40, beh_w: 0.60, epsilon: 0.12,
  recency_half_life_days: 30, min_plays_for_signal: 3, recent_ring_size: 50,
  drift_alpha: 0.25, skip_streak_transition: 2, chapter_max_tracks: 6,
  target_far_z: -1.0, bridge_min_z: 0.0, dj_epsilon: 0.08
};

const CFG_WEIGHTS = [
  { key:'audio_w',  label:'Audio similarity weight', desc:'Cosine distance between track embeddings', min:0, max:1, step:.01 },
  { key:'beh_w',    label:'Behavioral weight',        desc:'Completion rate × recency × context affinity', min:0, max:1, step:.01 },
];
const CFG_REC = [
  { key:'epsilon',          label:'Exploration rate (ε)', desc:'Probability of picking a random track instead of cosine-similar ones', min:0, max:.5, step:.01 },
  { key:'recent_ring_size', label:'Ring buffer size',      desc:'Number of recently-played tracks to exclude from all candidate pools', min:10, max:200, step:5 },
];
const CFG_BEH = [
  { key:'recency_half_life_days', label:'Recency half-life',     desc:'Days until a play counts half as much (exponential decay)', min:1, max:180, step:1 },
  { key:'min_plays_for_signal',   label:'Min plays for signal',   desc:'Play count threshold before track-level stats are trusted over artist-level', min:1, max:20, step:1 },
];
const CFG_DJ = [
  { key:'drift_alpha',            label:'Centroid drift',         desc:'How far the sonic center moves toward each completed track', min:0, max:.6, step:.05 },
  { key:'skip_streak_transition', label:'Skips before transition',desc:'Consecutive skips that force the DJ to change neighborhoods', min:1, max:5, step:1 },
  { key:'chapter_max_tracks',     label:'Chapter length',         desc:'Tracks per chapter before a proactive transition (avoid boredom)', min:3, max:12, step:1 },
  { key:'target_far_z',           label:'Transition distance (z)',desc:'How far (std-devs below mean similarity) a new neighborhood must be', min:-2.5, max:0, step:.1 },
  { key:'bridge_min_z',           label:'Bridge quality gate (z)',desc:'Min z-score of a bridge track\\'s similarity to both neighborhoods', min:-1, max:1.5, step:.1 },
  { key:'dj_epsilon',             label:'DJ exploration',         desc:'Chance of an off-neighborhood explore pick within a chapter', min:0, max:.3, step:.01 },
];

let cfg = {...DEFAULTS};

function fmt(d, v) { return d.step < 1 ? Number(v).toFixed(2) : Number(v).toFixed(0); }

function renderGroup(id, defs) {
  document.getElementById(id).innerHTML = defs.map(d => {
    const v = cfg[d.key] !== undefined ? cfg[d.key] : DEFAULTS[d.key];
    return `<div class="cfg-row">
      <div><label>${d.label}</label><div class="desc">${d.desc}</div></div>
      <input type="range" min="${d.min}" max="${d.max}" step="${d.step}" value="${v}"
        oninput="cfg['${d.key}']=+this.value; this.closest('.cfg-row').querySelector('.num').textContent=fmt(${JSON.stringify(d)},+this.value)">
      <div class="num">${fmt(d, v)}</div>
    </div>`;
  }).join('');
}

async function load() {
  try {
    const [health, stats] = await Promise.all([
      fetch('/api/health').then(r=>r.json()),
      fetch('/api/admin/stats').then(r=>r.json())
    ]);
    const dot = document.getElementById('status-dot');
    dot.className = health.ok ? '' : 'err';
    document.getElementById('status-text').textContent = health.ok ? 'running' : 'not ready';
    document.getElementById('s-status').textContent  = health.ok ? '●  online' : '○  offline';
    document.getElementById('s-status').style.color  = health.ok ? 'var(--green)' : 'var(--red)';
    document.getElementById('s-status').style.fontSize = '16px';
    document.getElementById('s-tracks').textContent = health.tracks.toLocaleString();
    document.getElementById('s-ctx').textContent    = stats.context_split.map(c=>c.context).join(' / ') || '—';
    document.getElementById('s-ring').textContent   = stats.ring_buffer.length;

    cfg = {...DEFAULTS, ...stats.config};
    renderGroup('cfg-weights', CFG_WEIGHTS);
    renderGroup('cfg-rec',     CFG_REC);
    renderGroup('cfg-beh',     CFG_BEH);
    renderGroup('cfg-dj',      CFG_DJ);

    const ring = stats.ring_buffer;
    document.getElementById('ring-label').textContent = `${ring.length} / ${cfg.recent_ring_size}`;
    const tags = document.getElementById('ring-tags');
    if (ring.length === 0) {
      tags.innerHTML = '<span class="ring-empty">empty</span>';
    } else {
      tags.innerHTML = ring.map(p => {
        const name = p.split(/[\\/]/).pop().replace(/\.\w+$/, '');
        return `<span class="ring-tag" title="${p}">${name}</span>`;
      }).join('');
    }
  } catch(e) {
    document.getElementById('status-dot').className = 'err';
    document.getElementById('status-text').textContent = 'error: ' + e.message;
  }
}

async function saveConfig() {
  const r = await fetch('/api/admin/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(cfg)
  });
  const d = await r.json();
  const msg = document.getElementById('save-msg');
  msg.textContent = d.ok ? '✓ Applied' : '✗ Error';
  msg.style.color = d.ok ? 'var(--green)' : 'var(--red)';
  setTimeout(() => msg.textContent = '', 2500);
}

function resetConfig() {
  cfg = {...DEFAULTS};
  renderGroup('cfg-weights', CFG_WEIGHTS);
  renderGroup('cfg-rec',     CFG_REC);
  renderGroup('cfg-beh',     CFG_BEH);
}

async function doAction(action) {
  const log = document.getElementById('action-log');
  log.style.color = 'var(--muted)';
  log.textContent = 'running…';
  try {
    const r = await fetch('/api/admin/action', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({action})
    });
    const d = await r.json();
    log.style.color = 'var(--green)';
    log.textContent = d.message || (d.ok ? '✓ Done' : '✗ Error');
  } catch(e) {
    log.style.color = 'var(--red)';
    log.textContent = '✗ ' + e.message;
  }
  await load();
  setTimeout(() => log.textContent = '', 4000);
}

load();
setInterval(load, 30000);
</script>
</body>
</html>"""


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    _startup()
    if not _ready:
        print("[startup] ABORT: embeddings not loaded — exiting")
        sys.exit(1)
    app.run(host=API_HOST, port=API_PORT, debug=False, threaded=True)
