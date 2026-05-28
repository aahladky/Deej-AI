"""
dj_session.py — Stateful "AI DJ" engine for DeeJAI

Server-owned DJ sessions: an independent curator that opens on time-of-day
affinity, holds conviction as it plays, drifts through the embedding space on
completions, and makes intentional *musical* transitions between sonic
neighborhoods via scored bridge tracks (with a multi-hop walk fallback).

This module is Flask-free and takes all of its dependencies by injection, so the
algorithms can be unit-tested against a small synthetic embedding matrix without
the 22k-track pickle or the web layer.

Dependencies injected into DJManager:
    vecs            np.ndarray (N, D), L2-normalized embedding rows
    path_index      dict[str, int]      abs path -> row in vecs
    all_paths       list[str]           row -> abs path
    scorer          object with .score_candidate(path, sim, meta, when),
                                        .top_tracks(when, n)
    track_meta      dict[str, dict]     abs path -> {artist,title,album}
    recent_ring     collections.deque   globally-recent paths to exclude
    find_seed       callable(artist, title) -> abs path | None  (also "seedable?")
    build_payload   callable(path, score) -> dict  (client track JSON)
    log_play        callable(artist,title,album,ms_played,completed,context,track_path)
    explore_cands   callable() -> list[(path, sim)] | None  (epsilon exploration)

See COMPONENT_PLAN_DJ_SESSION_API.md for the full spec.
"""

import datetime
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from uuid import uuid4

import numpy as np

# --------------------------------------------------------------------------- #
# Tunable constants  (later surfacable as admin sliders — Phase B)
# --------------------------------------------------------------------------- #

DRIFT_ALPHA            = 0.25   # centroid step toward a completed track
SKIP_STREAK_TRANSITION = 2      # consecutive skips that force a transition
CHAPTER_MAX_TRACKS     = 6      # proactive transition after this many in a chapter
# Similarity thresholds are expressed as z-scores against the library's OWN
# cosine distribution (estimated at init), because mp3tovec embeddings live in a
# tight cone (random-pair cosine ≈ 0.95 ± 0.03; nothing is below ~0.6). Absolute
# cutoffs like 0.5 are meaningless here and don't survive embedding retraining.
TARGET_FAR_Z           = -1.0   # a transition target's similarity to C must be at
                                # least this many std-devs BELOW the library mean
TARGET_RELAX_Z         = -0.3   # relaxed target threshold when none qualify
BRIDGE_MIN_Z           =  0.0   # gate: z(min(sim_c,sim_t)) a bridge must clear;
                                # also walk coherence + the walk's "arrived" test
MAX_TRANSITION_STEPS   = 3      # cap on the multi-hop walk
DJ_EPSILON             = 0.08   # chance of an off-neighborhood explore pick
AUDIO_POOL_SIZE        = 50     # candidates re-ranked per in-chapter pick
SAMPLE_TOPK            = 8       # softmax sample over this many top candidates
SAMPLE_TEMP            = 12.0    # softmax sharpness (higher = greedier)
SESSION_TTL            = 7200.0  # seconds of inactivity before a session is pruned


def _now_hour() -> float:
    """Current local hour-of-day as a float in [0, 24). Matches scorer.now_hour()."""
    now = datetime.datetime.now()
    return now.hour + now.minute / 60.0


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #

@dataclass
class DJSession:
    session_id:        str
    centroid:          np.ndarray              # L2-normalized; current sonic center
    chapter:           int   = 1               # 1-based
    chapter_len:       int   = 0               # tracks served in current chapter
    skip_streak:       int   = 0
    completion_streak: int   = 0
    played:            set   = field(default_factory=set)
    current_path:      str   = ''
    hour:              Optional[float] = None   # None => live clock each call
    # multi-hop transition walk state
    target:            Optional[np.ndarray] = None
    transition_steps:  int   = 0
    created_at:        float = field(default_factory=time.time)
    last_active_at:    float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #

class DJManager:
    def __init__(
        self,
        vecs:          np.ndarray,
        path_index:    Dict[str, int],
        all_paths:     List[str],
        scorer,
        track_meta:    Dict[str, dict],
        recent_ring,
        find_seed:     Callable[[str, str], Optional[str]],
        build_payload: Callable[[str, float], dict],
        log_play:      Callable[..., str],
        explore_cands: Optional[Callable[[], list]] = None,
    ):
        self.vecs          = vecs
        self.path_index    = path_index
        self.all_paths     = all_paths
        self.scorer        = scorer
        self.track_meta    = track_meta
        self.recent_ring   = recent_ring
        self.find_seed     = find_seed
        self.build_payload = build_payload
        self.log_play      = log_play
        self.explore_cands = explore_cands
        self.sessions: Dict[str, DJSession] = {}
        # Calibrate similarity thresholds to this library's own cosine spread.
        self._sim_mu, self._sim_sigma = self._estimate_sim_distribution()

    def _estimate_sim_distribution(self, n: int = 40000) -> Tuple[float, float]:
        """Mean/std of cosine similarity between random track pairs. Used to turn
        the z-score thresholds into absolute cutoffs for this embedding space."""
        N = len(self.all_paths)
        if N < 2:
            return 0.0, 1.0
        rng = np.random.default_rng(0)
        i = rng.integers(0, N, n)
        j = rng.integers(0, N, n)
        cs = np.sum(self.vecs[i] * self.vecs[j], axis=1)
        mu = float(cs.mean())
        sd = float(cs.std())
        return mu, (sd if sd > 1e-6 else 1.0)

    def _sim_cut(self, z: float) -> float:
        """Absolute cosine cutoff for a z-score against the library distribution."""
        return self._sim_mu + z * self._sim_sigma

    # ----- vector helpers ------------------------------------------------- #

    def _vec(self, path: str) -> np.ndarray:
        return self.vecs[self.path_index[path]]

    @staticmethod
    def _unit(v: np.ndarray) -> np.ndarray:
        n = float(np.linalg.norm(v))
        return v if n == 0.0 else (v / n)

    def _resolve_hour(self, s: DJSession) -> float:
        return s.hour if s.hour is not None else _now_hour()

    # ----- public API ----------------------------------------------------- #

    def start(self, hour: Optional[float] = None,
              seed_artist: str = '', seed_title: str = '') -> Optional[dict]:
        """Open a session. Returns the opening-track payload, or None if no seed
        could be found (caller maps None -> 503/500)."""
        self._prune()
        seed = None
        if seed_artist or seed_title:
            seed = self.find_seed(seed_artist, seed_title)
        if seed is None:
            seed = self._seed_by_time(hour if hour is not None else _now_hour())
        if seed is None or seed not in self.path_index:
            return None

        s = DJSession(
            session_id=uuid4().hex,
            centroid=self._unit(self._vec(seed)).copy(),
            hour=hour,
            current_path=seed,
            played={seed},
            chapter_len=1,
        )
        self.sessions[s.session_id] = s
        score = self._display_score(s, seed)
        out = self._payload(s, seed, 'opening', False, score)
        out['next_preview'] = self._peek_track(s)
        return out

    def next(self, session_id: str, completed: bool, ms_played: int) -> Optional[dict]:
        """Record feedback on the track just served and return the next one.
        Returns None if the session is unknown/expired (caller -> 404)."""
        s = self.sessions.get(session_id)
        if s is None:
            return None
        s.last_active_at = time.time()

        # 1. log the finished track (never break the session over a write)
        meta = self.track_meta.get(s.current_path, {})
        try:
            self.log_play(meta.get('artist', ''), meta.get('title', ''),
                          meta.get('album', ''), int(ms_played),
                          int(bool(completed)), None, s.current_path)
        except Exception:
            pass

        # 2. update streaks + centroid (conviction model)
        if completed:
            s.completion_streak += 1
            s.skip_streak = 0
            v = self._unit(self._vec(s.current_path))
            s.centroid = self._unit((1.0 - DRIFT_ALPHA) * s.centroid + DRIFT_ALPHA * v)
        else:
            s.skip_streak += 1
            s.completion_streak = 0

        # 3. choose the next track
        path, reason, transition, score = self._advance(s)
        if path is None:
            return None  # truly exhausted (should be unreachable in practice)
        s.current_path = path
        s.played.add(path)
        out = self._payload(s, path, reason, transition, score)
        out['next_preview'] = self._peek_track(s)
        return out

    def end(self, session_id: str) -> bool:
        """Drop a session explicitly (frees memory before TTL). True if it existed."""
        return self.sessions.pop(session_id, None) is not None

    def inspect(self, session_id: str) -> Optional[dict]:
        s = self.sessions.get(session_id)
        if s is None:
            return None
        sims = self.vecs @ s.centroid
        top_idx = np.argsort(-sims)[:5]
        centroid_top = []
        for i in top_idx:
            m = self.track_meta.get(self.all_paths[i], {})
            centroid_top.append({'artist': m.get('artist', ''),
                                 'title': m.get('title', ''),
                                 'sim': round(float(sims[i]), 3)})
        return {
            'session_id': s.session_id, 'chapter': s.chapter,
            'chapter_len': s.chapter_len, 'skip_streak': s.skip_streak,
            'completion_streak': s.completion_streak, 'hour': self._resolve_hour(s),
            'played_count': len(s.played), 'in_transition': s.target is not None,
            'centroid_top': centroid_top,
        }

    # ----- advance / transition logic ------------------------------------ #

    def _advance(self, s: DJSession) -> Tuple[Optional[str], str, bool, float]:
        hour = self._resolve_hour(s)

        # continue an in-progress multi-hop walk
        if s.target is not None:
            return self._walk_step(s, hour)

        # decide whether to transition
        if s.skip_streak >= SKIP_STREAK_TRANSITION:
            return self._begin_transition(s, hour, 'recover')
        if s.chapter_len >= CHAPTER_MAX_TRACKS:
            return self._begin_transition(s, hour, 'new_chapter')

        # normal in-chapter pick
        path, score = self._pick_in_neighborhood(s, hour)
        if path is None:
            return None, 'continue', False, 0.0
        s.chapter_len += 1
        reason = 'deepen' if s.completion_streak > 0 else 'continue'
        return path, reason, False, score

    def _begin_transition(self, s: DJSession, hour: float,
                          base_reason: str) -> Tuple[Optional[str], str, bool, float]:
        target = self._choose_target(s, hour, relax=False)
        if target is None:
            return self._stay(s, hour)

        bridge = self._best_bridge(s, s.centroid, target)
        if bridge is not None:
            return self._arrive(s, target, bridge, hour)

        # fallback: try to take one coherent step toward the target
        step = self._best_walk_step(s, s.centroid, target)
        if step is None:
            # sparse region: try a nearer target once, else stay
            target = self._choose_target(s, hour, relax=True)
            if target is not None:
                bridge = self._best_bridge(s, s.centroid, target)
                if bridge is not None:
                    return self._arrive(s, target, bridge, hour)
                step = self._best_walk_step(s, s.centroid, target)
            if step is None:
                return self._stay(s, hour)

        # begin the walk — this step opens the new chapter
        s.target = target
        s.transition_steps = 1
        s.centroid = self._unit(self._vec(step)).copy()
        s.skip_streak = 0
        s.chapter += 1
        s.chapter_len = 1
        return step, 'bridge_step', True, self._display_score(s, step)

    def _walk_step(self, s: DJSession, hour: float) -> Tuple[Optional[str], str, bool, float]:
        arrived = (float(s.target @ s.centroid) >= self._sim_cut(BRIDGE_MIN_Z)
                   or s.transition_steps >= MAX_TRANSITION_STEPS)
        if arrived:
            s.centroid = self._unit(s.target).copy()
            s.target = None
            s.transition_steps = 0
            path, score = self._pick_in_neighborhood(s, hour)
            if path is None:
                return None, 'continue', False, 0.0
            s.chapter_len += 1
            return path, 'continue', False, score

        step = self._best_walk_step(s, s.centroid, s.target)
        if step is None:
            # can't make further progress — adopt where we are
            s.centroid = self._unit(s.target).copy()
            s.target = None
            s.transition_steps = 0
            path, score = self._pick_in_neighborhood(s, hour)
            if path is None:
                return None, 'continue', False, 0.0
            s.chapter_len += 1
            return path, 'continue', False, score

        s.transition_steps += 1
        s.centroid = self._unit(self._vec(step)).copy()
        s.chapter_len += 1
        return step, 'bridge_step', True, self._display_score(s, step)

    def _arrive(self, s: DJSession, target: np.ndarray, bridge: str,
                hour: float) -> Tuple[str, str, bool, float]:
        """Clean one-track bridge: serve it and adopt the target neighborhood."""
        s.centroid = self._unit(target).copy()
        s.target = None
        s.transition_steps = 0
        s.skip_streak = 0
        s.chapter += 1
        s.chapter_len = 1
        return bridge, 'bridge', True, self._display_score(s, bridge)

    def _stay(self, s: DJSession, hour: float) -> Tuple[Optional[str], str, bool, float]:
        """No usable transition target/bridge — stay in the current neighborhood."""
        path, score = self._pick_in_neighborhood(s, hour)
        if path is None:
            return None, 'continue', False, 0.0
        s.chapter_len += 1
        return path, 'continue', False, score

    # ----- candidate selection ------------------------------------------- #

    def _choose_target(self, s: DJSession, hour: float,
                       relax: bool = False) -> Optional[np.ndarray]:
        far_cut = self._sim_cut(TARGET_RELAX_Z if relax else TARGET_FAR_Z)
        tops = self.scorer.top_tracks(hour, 40)
        candidates = []
        for a, t, _ in tops:
            p = self.find_seed(a, t)
            if not p or p in s.played or p in self.recent_ring or p not in self.path_index:
                continue
            v = self._unit(self._vec(p))
            if float(v @ s.centroid) <= far_cut:
                candidates.append(v)
        if candidates:
            return candidates[np.random.randint(len(candidates))].copy()
        # last resort: any seedable top track at all
        for a, t, _ in tops:
            p = self.find_seed(a, t)
            if p and p in self.path_index and p not in s.played:
                return self._unit(self._vec(p)).copy()
        return None

    def _best_bridge(self, s: DJSession, C: np.ndarray,
                     T: np.ndarray) -> Optional[str]:
        """Best track between C and T by min(sim_c, sim_t); returns it only if it
        clears BRIDGE_MIN_SIM, else None. Tie-break by sim_c + sim_t."""
        gate = self._sim_cut(BRIDGE_MIN_Z)
        sims_c = self.vecs @ C
        sims_t = self.vecs @ T
        m = np.minimum(sims_c, sims_t)
        key = m + 1e-3 * (sims_c + sims_t)        # tie-break toward overall-closer
        for i in np.argsort(-key):
            p = self.all_paths[i]
            if p in s.played or p in self.recent_ring or p == s.current_path:
                continue
            return p if m[i] >= gate else None
        return None

    def _best_walk_step(self, s: DJSession, C: np.ndarray,
                        T: np.ndarray) -> Optional[str]:
        """Furthest-toward-T track that still coheres with C (sim_c >= gate) and
        actually makes progress (closer to T than C currently is). Else None."""
        gate = self._sim_cut(BRIDGE_MIN_Z)
        sims_c = self.vecs @ C
        sims_t = self.vecs @ T
        current_sim_t = float(T @ C)
        mask = sims_c >= gate
        if not mask.any():
            return None
        cand_t = np.where(mask, sims_t, -np.inf)
        for i in np.argsort(-cand_t):
            if not mask[i]:
                return None
            p = self.all_paths[i]
            if p in s.played or p in self.recent_ring or p == s.current_path:
                continue
            if sims_t[i] <= current_sim_t:
                return None  # best coherent candidate doesn't move us toward T
            return p
        return None

    def _centroid_candidates(self, centroid: np.ndarray, played: set,
                             ignore_ring: bool = False) -> List[Tuple[str, float]]:
        sims = self.vecs @ centroid
        k = min(AUDIO_POOL_SIZE + len(self.recent_ring) + 60, len(self.all_paths))
        idx = np.argpartition(sims, -k)[-k:]
        out = []
        for i in idx:
            p = self.all_paths[i]
            if p in played:
                continue
            if not ignore_ring and p in self.recent_ring:
                continue
            out.append((p, float(sims[i])))
        out.sort(key=lambda x: -x[1])
        return out[:AUDIO_POOL_SIZE]

    def _pick_in_neighborhood(self, s: DJSession,
                              hour: float) -> Tuple[Optional[str], float]:
        cands: List[Tuple[str, float]] = []
        if self.explore_cands is not None and np.random.random() < DJ_EPSILON:
            cands = self.explore_cands() or []
            cands = [(p, sim) for p, sim in cands
                     if p not in s.played and p not in self.recent_ring]
        if not cands:
            cands = self._centroid_candidates(s.centroid, s.played)
        if not cands:
            cands = self._centroid_candidates(s.centroid, s.played, ignore_ring=True)
        if not cands:
            return None, 0.0
        scored = [(p, self.scorer.score_candidate(p, sim, self.track_meta, hour))
                  for p, sim in cands]
        scored.sort(key=lambda x: -x[1])
        return self._weighted_pick(scored[:SAMPLE_TOPK])

    @staticmethod
    def _weighted_pick(scored: List[Tuple[str, float]]) -> Tuple[Optional[str], float]:
        if not scored:
            return None, 0.0
        paths = [p for p, _ in scored]
        sc = np.array([v for _, v in scored], dtype=np.float64)
        w = np.exp((sc - sc.max()) * SAMPLE_TEMP)
        total = w.sum()
        if total <= 0 or not np.isfinite(total):
            i = 0
        else:
            i = int(np.random.choice(len(paths), p=w / total))
        return paths[i], float(sc[i])

    # ----- seeding -------------------------------------------------------- #

    def _seed_by_time(self, hour: float) -> Optional[str]:
        tops = self.scorer.top_tracks(hour, 40)
        pool = [(self.find_seed(a, t), sc) for a, t, sc in tops]
        pool = [(p, sc) for p, sc in pool
                if p and p in self.path_index and p not in self.recent_ring]
        if not pool:
            pool = [(self.find_seed(a, t), sc) for a, t, sc in tops]
            pool = [(p, sc) for p, sc in pool if p and p in self.path_index]
        if not pool:
            return None
        cut = pool[:12]
        paths = [p for p, _ in cut]
        sc = np.array([max(v, 1e-6) for _, v in cut], dtype=np.float64)
        return paths[int(np.random.choice(len(paths), p=sc / sc.sum()))]

    # ----- misc ----------------------------------------------------------- #

    def _display_score(self, s: DJSession, path: str) -> float:
        try:
            return float(self.scorer.score_candidate(path, 1.0, self.track_meta,
                                                      self._resolve_hour(s)))
        except Exception:
            return 0.0

    def _payload(self, s: DJSession, path: str, reason: str,
                 transition: bool, score: float) -> dict:
        return {
            'session_id': s.session_id,
            'chapter':    s.chapter,
            'reason':     reason,
            'transition': transition,
            'track':      self.build_payload(path, score),
        }

    def _peek_track(self, s: DJSession) -> Optional[dict]:
        """Non-binding hint of the likely next track for the current centroid, so
        the client can buffer one ahead. Does not mutate state and does not model
        a future transition — purely a prefetch convenience."""
        cands = self._centroid_candidates(s.centroid, s.played)
        if not cands:
            return None
        hour = self._resolve_hour(s)
        best_p, best_sc = None, -1e9
        for p, sim in cands:
            sc = self.scorer.score_candidate(p, sim, self.track_meta, hour)
            if sc > best_sc:
                best_p, best_sc = p, sc
        return self.build_payload(best_p, best_sc) if best_p else None

    def _prune(self):
        cutoff = time.time() - SESSION_TTL
        dead = [sid for sid, s in self.sessions.items() if s.last_active_at < cutoff]
        for sid in dead:
            del self.sessions[sid]
