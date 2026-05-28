import pickle, re, json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

META_PATH = r'C:\Dev\DeeJAI\radio\track_meta.json'

_PUNCT = re.compile(r"[^\w\s]")
_WS    = re.compile(r"\s+")
def norm(s):
    s = (s or '').lower().strip()
    s = _PUNCT.sub('', s)
    return _WS.sub(' ', s).strip()

with open(META_PATH) as f:
    cached = json.load(f)

# All Clint Black tracks in the embedding index
print("Clint Black tracks in embedding index:")
for p, v in cached.items():
    if norm(v.get('artist','')) == 'clint black':
        print(f"  {v['title']!r}")

# How many total tracks per artist (top 10)
from collections import Counter
artists = Counter(norm(v.get('artist','')) for v in cached.values())
print("\nTop artists by embedding count:")
for a, c in artists.most_common(10):
    print(f"  {c:3d}  {a}")
