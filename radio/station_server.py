#!/usr/bin/env python3
"""
DeejAI Radio Station Server
Plays sonically-similar music locally via VLC based on a seed song, artist, or genre.
Web UI:  http://localhost:8051
Plays audio directly through your default audio device.
"""

import os, sys, json, pickle, time, threading, subprocess, sqlite3, datetime
from collections import deque
import numpy as np

from scorer import Scorer

try:
    from flask import Flask, request, jsonify
except ImportError:
    print("Flask not found. Run: pip install flask")
    sys.exit(1)

try:
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    from mutagen.id3 import ID3, ID3NoHeaderError
except ImportError:
    print("mutagen not found. Run: pip install mutagen")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
MP3TOVECS_FILE  = r'C:\Dev\DeeJAI\pickles\mp3tovecs\mp3tovecs.p'
META_CACHE      = r'C:\Dev\DeeJAI\radio\track_meta.json'
DB_PATH         = r'C:\Dev\DeeJAI\radio\plays.db'
VLC             = r'C:\Program Files (x86)\VideoLAN\VLC\vlc.exe'
LOOKBACK        = 50      # recent tracks to avoid repeating
CENTROID_DECAY  = 0.15    # how quickly sound drifts toward played tracks
PORT            = 8051

# Gym context: weekday 16:30–21:00 local time
GYM_DAYS    = {0, 1, 2, 3, 4}
GYM_START_H = 16.5
GYM_END_H   = 21.0

# ── Load embeddings ───────────────────────────────────────────────────────────
print("Loading embeddings...")
mp3tovecs  = pickle.load(open(MP3TOVECS_FILE, 'rb'))
all_paths  = list(mp3tovecs.keys())
print(f"Loaded {len(all_paths)} tracks")

all_vecs      = np.array([mp3tovecs[p] for p in all_paths], dtype=np.float32)
norms         = np.maximum(np.linalg.norm(all_vecs, axis=1, keepdims=True), 1e-8)
all_vecs_norm = (all_vecs / norms).astype(np.float32)
path_to_idx   = {p: i for i, p in enumerate(all_paths)}
print("Similarity index built")

# ── Tag scanning ─────────────────────────────────────────────────────────────
def read_tags(path):
    artist = title = album = genre = None
    try:
        ext = path.lower().rsplit('.', 1)[-1]
        if ext == 'mp3':
            try:
                tags = ID3(path)
                if tags.get('TPE1'): artist = str(tags['TPE1'].text[0])
                if tags.get('TIT2'): title  = str(tags['TIT2'].text[0])
                if tags.get('TALB'): album  = str(tags['TALB'].text[0])
                if tags.get('TCON'): genre  = str(tags['TCON'].text[0])
            except ID3NoHeaderError:
                pass
        elif ext == 'flac':
            tags = FLAC(path)
            def g(k): return (tags.get(k.lower()) or tags.get(k.upper()) or [None])[0]
            artist, title, album, genre = g('artist'), g('title'), g('album'), g('genre')
        elif ext == 'm4a':
            tags = MP4(path)
            def gm(k): return (tags.get(k) or [None])[0]
            artist = gm('\xa9ART'); title = gm('\xa9nam')
            album  = gm('\xa9alb'); genre = gm('\xa9gen')
    except Exception:
        pass
    return {
        'artist': str(artist) if artist else os.path.basename(os.path.dirname(path)),
        'title':  str(title)  if title  else os.path.splitext(os.path.basename(path))[0],
        'album':  str(album)  if album  else '',
        'genre':  str(genre)  if genre  else '',
    }

def load_metadata():
    if os.path.exists(META_CACHE):
        print("Loading cached metadata...")
        with open(META_CACHE, 'r', encoding='utf-8') as f:
            return json.load(f)
    print(f"Scanning tags for {len(all_paths)} tracks (first run, ~1 min)...")
    meta = {}
    for i, path in enumerate(all_paths):
        meta[path] = read_tags(path)
        if (i + 1) % 500 == 0:
            print(f"  {i+1}/{len(all_paths)}")
    os.makedirs(os.path.dirname(META_CACHE), exist_ok=True)
    with open(META_CACHE, 'w', encoding='utf-8') as f:
        json.dump(meta, f)
    print("Metadata cached.")
    return meta

track_meta = load_metadata()

artist_tracks = {}
genre_tracks  = {}
for path, m in track_meta.items():
    if path not in path_to_idx:
        continue
    a = (m.get('artist') or '').strip()
    g = (m.get('genre')  or '').strip()
    if a: artist_tracks.setdefault(a.lower(), []).append(path)
    if g: genre_tracks.setdefault(g.lower(),  []).append(path)

print(f"Indexed {len(artist_tracks)} artists, {len(genre_tracks)} genres")

# ── Context detection ─────────────────────────────────────────────────────────
def get_context() -> str:
    now  = datetime.datetime.now()
    hour = now.hour + now.minute / 60
    if now.weekday() in GYM_DAYS and GYM_START_H <= hour <= GYM_END_H:
        return 'gym'
    return 'home'

# ── SQLite play logging ───────────────────────────────────────────────────────
def _init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute('''CREATE TABLE IF NOT EXISTS plays (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        track_path  TEXT,
        artist      TEXT,
        title       TEXT,
        album       TEXT,
        started_at  REAL,
        ms_played   INTEGER,
        completed   INTEGER,
        context     TEXT
    )''')
    con.commit()
    con.close()

def log_play(track: str, meta: dict, ms_played: int, completed: bool):
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute('INSERT INTO plays VALUES (NULL,?,?,?,?,?,?,?,?)', (
            track, meta.get('artist',''), meta.get('title',''), meta.get('album',''),
            time.time(), ms_played, int(completed), get_context(),
        ))
        con.commit()
        con.close()
    except Exception as e:
        print(f'DB error: {e}', flush=True)

_init_db()

# Kill any orphaned VLC instances from a previous server session
subprocess.run(['taskkill', '/f', '/im', 'vlc.exe'],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ── Radio Station ─────────────────────────────────────────────────────────────
class RadioStation:
    def __init__(self):
        self.centroid   = None
        self.recently   = deque(maxlen=LOOKBACK)
        self.current    = None
        self.proc       = None
        self.play_start = None
        self.seed_track = None   # play this first when seeding from a song
        self._lock      = threading.Lock()
        self._gen       = 0

    def _seed_embedding(self, seed_type, seed_value):
        if seed_type == 'song':
            if seed_value in path_to_idx:
                return all_vecs_norm[path_to_idx[seed_value]].copy()
        elif seed_type == 'artist':
            q = seed_value.lower()
            paths = artist_tracks.get(q, [])
            if not paths:
                for a, ps in artist_tracks.items():
                    if q in a: paths = ps; break
            if paths:
                idxs = [path_to_idx[p] for p in paths if p in path_to_idx]
                if idxs:
                    v = all_vecs_norm[idxs].mean(axis=0)
                    n = np.linalg.norm(v)
                    return v / n if n > 0 else None
        elif seed_type == 'genre':
            q = seed_value.lower()
            paths = genre_tracks.get(q, [])
            if not paths:
                for g, ps in genre_tracks.items():
                    if q in g: paths = ps; break
            if paths:
                idxs = [path_to_idx[p] for p in paths if p in path_to_idx]
                if idxs:
                    v = all_vecs_norm[idxs].mean(axis=0)
                    n = np.linalg.norm(v)
                    return v / n if n > 0 else None
        return None

    def _next_track(self):
        recently_set = set(self.recently)
        sims = all_vecs_norm @ self.centroid
        for p in recently_set:
            if p in path_to_idx:
                sims[path_to_idx[p]] = -2.0

        # Pull top-50 audio candidates, then let the scorer re-rank them
        top_n = 50
        top_idxs = np.argpartition(sims, -top_n)[-top_n:]
        candidates = [(all_paths[i], float(sims[i])) for i in top_idxs]
        return scorer.rescore_candidates(candidates, track_meta)

    def start(self, seed_type, seed_value):
        emb = self._seed_embedding(seed_type, seed_value)
        if emb is None:
            return False, f"Couldn't find '{seed_value}' in library"
        with self._lock:
            if self.proc:
                self.proc.kill()
            self._gen += 1
            gen = self._gen
            self.centroid   = emb.copy()
            self.recently.clear()
            self.current    = None
            self.play_start = None
            self.seed_track = seed_value if seed_type == 'song' else None
        threading.Thread(target=self._play_loop, args=(gen,), daemon=True).start()
        return True, 'ok'

    def skip(self):
        with self._lock:
            if self.proc:
                self.proc.kill()

    def stop(self):
        with self._lock:
            self._gen += 1
            if self.proc:
                self.proc.kill()
            self.current    = None
            self.play_start = None

    def _play_loop(self, gen):
        """Play tracks locally via VLC, log each play event to SQLite."""
        def alive(): return self._gen == gen
        first = True

        while alive():
            with self._lock:
                if not alive(): break
                if first and self.seed_track:
                    track = self.seed_track
                    self.seed_track = None
                else:
                    track = self._next_track()
                first = False
                self.recently.append(track)
                self.current    = track
                self.play_start = time.time()

            m = track_meta.get(track, {})
            ctx = get_context()
            print(f"  ▶ [{ctx}] {m.get('artist','?')} — {m.get('title','?')}", flush=True)

            # VLC headless playback — blocks until track ends or is killed
            cmd = [VLC, '--intf', 'dummy', '--no-video', '--play-and-exit',
                   '--no-qt-system-tray', '--qt-start-minimized', track]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with self._lock:
                self.proc = proc

            proc.wait()

            elapsed_ms = int((time.time() - self.play_start) * 1000)
            completed  = (proc.returncode == 0)
            log_play(track, m, elapsed_ms, completed)
            scorer.on_play_logged()
            print(f"     {'✓' if completed else '⏭'} {elapsed_ms//1000}s", flush=True)

            # Drift centroid only on completions
            if alive() and completed and track in path_to_idx:
                with self._lock:
                    v = all_vecs_norm[path_to_idx[track]]
                    c = (1 - CENTROID_DECAY) * self.centroid + CENTROID_DECAY * v
                    n = np.linalg.norm(c)
                    if n > 0:
                        self.centroid = c / n

        with self._lock:
            self.current    = None
            self.play_start = None

    def status(self):
        m       = track_meta.get(self.current, {}) if self.current else {}
        elapsed = int((time.time() - self.play_start) * 1000) if self.play_start and self.current else 0
        return {
            'running':    self.current is not None,
            'track':      self.current or '',
            'artist':     m.get('artist', ''),
            'title':      m.get('title',  ''),
            'album':      m.get('album',  ''),
            'elapsed_ms': elapsed,
            'context':    get_context(),
        }

station = RadioStation()
scorer  = Scorer(db_path=DB_PATH)
print(f"Scorer loaded: {len(scorer._stats)} artists in behavioral cache")

# ── Flask routes ──────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('DEEJAY_SECRET_KEY', 'deejai-local-review-secret')

# Phase 3: register the manual review dashboard blueprint (/admin/review).
# Guarded so a blueprint import error can never crash the radio server.
try:
    if r'C:\Dev' not in sys.path:
        sys.path.insert(0, r'C:\Dev')
    from admin.review_routes import review_bp
    app.register_blueprint(review_bp)
    print("Review dashboard registered at /admin/review")
    from admin.genre_routes import genre_bp
    app.register_blueprint(genre_bp)
    print("Genre dashboard registered at /admin/genres")
except Exception as _e:
    print(f"[warn] admin dashboard not loaded: {_e}")

@app.route('/')
def index():
    return HTML

@app.route('/search')
def search():
    q = request.args.get('q', '').lower().strip()
    t = request.args.get('type', 'song')
    if len(q) < 2:
        return jsonify([])
    results = []
    if t == 'song':
        # Three tiers: title starts-with, title contains, artist contains
        tier1, tier2, tier3 = [], [], []
        for path in all_paths:
            m      = track_meta.get(path, {})
            title  = (m.get('title')  or '').lower()
            artist = (m.get('artist') or '').lower()
            entry  = {'label': m.get('title',''), 'sub': m.get('artist',''), 'value': path}
            if title.startswith(q):
                tier1.append(entry)
            elif q in title:
                tier2.append(entry)
            elif q in artist:
                tier3.append(entry)
        results = (tier1 + tier2 + tier3)[:12]
    elif t == 'artist':
        tier1, tier2 = [], []
        for a in sorted(artist_tracks):
            if a.startswith(q):
                tier1.append({'label': a.title(), 'value': a})
            elif q in a:
                tier2.append({'label': a.title(), 'value': a})
        results = (tier1 + tier2)[:12]
    elif t == 'genre':
        for g in sorted(genre_tracks):
            if q in g:
                results.append({'label': g.title(), 'value': g})
                if len(results) >= 12: break
    return jsonify(results)

@app.route('/genres')
def genres():
    return jsonify(sorted(g.title() for g in genre_tracks))

@app.route('/start', methods=['POST'])
def start():
    d = request.json or {}
    ok, msg = station.start(d.get('type'), d.get('value', ''))
    return jsonify({'ok': ok, 'msg': msg})

@app.route('/skip', methods=['POST'])
def skip():
    station.skip()
    return jsonify({'ok': True})

@app.route('/stop', methods=['POST'])
def stop():
    station.stop()
    return jsonify({'ok': True})

@app.route('/status')
def status():
    return jsonify(station.status())


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DeejAI</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#111827;color:#f3f4f6;font-family:system-ui,sans-serif;min-height:100vh;display:flex;justify-content:center;align-items:center}
.card{background:#1f2937;border-radius:20px;padding:36px;width:440px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
h1{font-size:1.5rem;font-weight:700;color:#f9fafb;margin-bottom:28px;text-align:center;letter-spacing:.04em}
h1 span{color:#818cf8}
.tabs{display:flex;gap:6px;margin-bottom:14px}
.tab{flex:1;padding:8px;border:none;border-radius:8px;background:#374151;color:#9ca3af;cursor:pointer;font-size:.85rem;font-weight:500;transition:all .15s}
.tab.active{background:#6366f1;color:#fff}
.tab:hover:not(.active){background:#4b5563;color:#f3f4f6}
.sw{position:relative;margin-bottom:14px}
#search{width:100%;padding:11px 14px;border-radius:10px;border:1.5px solid #374151;background:#374151;color:#f3f4f6;font-size:.95rem;transition:border .15s}
#search:focus{outline:none;border-color:#6366f1}
#results{position:absolute;top:calc(100% + 4px);left:0;right:0;background:#2d3748;border-radius:10px;z-index:20;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.4);max-height:220px;overflow-y:auto}
.ri{padding:10px 14px;cursor:pointer;font-size:.88rem;border-bottom:1px solid #374151}
.ri:last-child{border-bottom:none}
.ri:hover,.ri.sel{background:#6366f1;color:#fff}
.ri .sub{font-size:.75rem;color:#9ca3af;margin-top:2px}
.ri:hover .sub,.ri.sel .sub{color:#c7d2fe}
.btns{display:flex;gap:8px;margin-bottom:20px}
.btn{flex:1;padding:12px;border:none;border-radius:10px;font-size:.95rem;font-weight:600;cursor:pointer;transition:all .15s}
.btn-start{background:#6366f1;color:#fff}.btn-start:hover:not(:disabled){background:#4f46e5}
.btn-start:disabled{background:#374151;color:#6b7280;cursor:default}
.btn-skip{background:#374151;color:#9ca3af;flex:0 0 100px}
.btn-skip:hover:not(:disabled){background:#4b5563;color:#f3f4f6}
.btn-skip:disabled{opacity:.35;cursor:default}
.np{border-top:1px solid #374151;padding-top:18px}
.np-lbl{font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;color:#6b7280;margin-bottom:10px;display:flex;align-items:center;gap:6px}
.dot{width:7px;height:7px;background:#6366f1;border-radius:50%;animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.75)}}
.np-title{font-size:1.05rem;font-weight:600;color:#f9fafb;margin-bottom:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.np-artist{font-size:.88rem;color:#a5b4fc;margin-bottom:2px}
.np-album{font-size:.78rem;color:#6b7280}
.np-bar{margin-top:10px;height:3px;background:#374151;border-radius:2px;overflow:hidden}
.np-bar-fill{height:100%;background:#6366f1;transition:width .5s linear;width:0%}
.ctx{margin-top:14px;text-align:center;font-size:.72rem}
.ctx span{display:inline-block;padding:3px 10px;border-radius:20px;font-weight:600;letter-spacing:.05em}
.gym{background:#064e3b;color:#6ee7b7}
.home{background:#1e3a5f;color:#93c5fd}
</style>
</head>
<body>
<div class="card">
  <h1>&#127925; <span>DeejAI</span></h1>
  <div class="tabs">
    <button class="tab active" onclick="setTab('song')">Song</button>
    <button class="tab" onclick="setTab('artist')">Artist</button>
    <button class="tab" onclick="setTab('genre')">Genre</button>
  </div>
  <div class="sw">
    <input id="search" type="text" placeholder="Search songs..." autocomplete="off" spellcheck="false">
    <div id="results"></div>
  </div>
  <div class="btns">
    <button id="btn-start" class="btn btn-start" disabled onclick="startStation()">Play</button>
    <button id="btn-skip" class="btn btn-skip" disabled onclick="skipTrack()">&#9197; Skip</button>
  </div>
  <div class="np" id="np"><div class="np-lbl">Not playing</div></div>
  <div class="ctx"><span id="ctx-badge" class="home">HOME</span></div>
</div>
<script>
let tab='song',selVal=null,debounce=null;
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function setTab(t){
  tab=t;selVal=null;
  document.querySelectorAll('.tab').forEach((el,i)=>el.classList.toggle('active',['song','artist','genre'][i]===t));
  const ph={song:'Search songs…',artist:'Search artists…',genre:'Search genres…'};
  const s=document.getElementById('search');s.placeholder=ph[t];s.value='';
  document.getElementById('results').innerHTML='';
  document.getElementById('btn-start').disabled=true;
  if(t==='genre')loadGenres();
}
document.getElementById('search').addEventListener('input',function(){
  clearTimeout(debounce);debounce=setTimeout(()=>doSearch(this.value),200);
});
document.getElementById('search').addEventListener('focus',function(){
  if(tab==='genre'&&!this.value)loadGenres();
});
async function loadGenres(){const gs=await(await fetch('/genres')).json();render(gs.map(g=>({label:g,value:g.toLowerCase()})));}
async function doSearch(q){
  if(!q.trim()){if(tab==='genre')loadGenres();else document.getElementById('results').innerHTML='';return;}
  if(q.length<2)return;
  render(await(await fetch('/search?q='+encodeURIComponent(q)+'&type='+tab)).json());
}
function render(items){
  const d=document.getElementById('results');
  if(!items.length){d.innerHTML='';return;}
  d.innerHTML=items.map(it=>{
    const div=document.createElement('div');
    div.className='ri';
    div.dataset.label=it.label||'';
    div.dataset.value=it.value||'';
    div.innerHTML=esc(it.label)+(it.sub?'<div class="sub">'+esc(it.sub)+'</div>':'');
    return div.outerHTML;
  }).join('');
  d.querySelectorAll('.ri').forEach(el=>{
    el.addEventListener('click',()=>pick(el.dataset.label,el.dataset.value));
  });
}
function pick(label,value){
  document.getElementById('search').value=label;
  document.getElementById('results').innerHTML='';
  selVal=value;document.getElementById('btn-start').disabled=false;
}
async function startStation(){
  if(!selVal)return;
  const d=await(await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:tab,value:selVal})})).json();
  if(!d.ok){alert(d.msg);return;}
  document.getElementById('btn-skip').disabled=false;
}
async function skipTrack(){await fetch('/skip',{method:'POST'});}
async function poll(){
  try{
    const d=await(await fetch('/status')).json();
    const badge=document.getElementById('ctx-badge');
    badge.className=d.context||'home';badge.textContent=(d.context||'home').toUpperCase();
    const el=document.getElementById('np');
    if(d.running&&d.title){
      const pct=Math.min(100,(d.elapsed_ms||0)/240000*100);
      el.innerHTML='<div class="np-lbl"><span class="dot"></span>Now Playing</div>'
        +'<div class="np-title">'+esc(d.title)+'</div>'
        +'<div class="np-artist">'+esc(d.artist)+'</div>'
        +'<div class="np-album">'+esc(d.album)+'</div>'
        +'<div class="np-bar"><div class="np-bar-fill" style="width:'+pct+'%"></div></div>';
      document.getElementById('btn-skip').disabled=false;
    }else if(!d.running){el.innerHTML='<div class="np-lbl">Not playing</div>';}
  }catch(e){}
}
setInterval(poll,2000);poll();
document.addEventListener('click',e=>{if(!e.target.closest('.sw'))document.getElementById('results').innerHTML='';});
</script>
</body>
</html>"""

if __name__ == '__main__':
    print(f"\nDeejAI  →  http://localhost:{PORT}")
    print(f"Plays:   {DB_PATH}\n")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
