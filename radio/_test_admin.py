import urllib.request, json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "http://localhost:8050"

# Test stats endpoint
r = urllib.request.urlopen(f"{BASE}/api/admin/stats")
d = json.loads(r.read())

print("=== SUMMARY ===")
for k, v in d['summary'].items():
    print(f"  {k}: {v}")

print("\n=== CONTEXT SPLIT ===")
for c in d['context_split']:
    print(f"  {c['context']}: {c['plays']} plays, {c['pct']}% complete")

print("\n=== TOP 5 ARTISTS ===")
for a in d['top_artists'][:5]:
    print(f"  {a['plays']:4d} plays  {a['pct']:5.1f}%  {a['artist']}")

print("\n=== TOP 5 TRACKS ===")
for t in d['top_tracks'][:5]:
    print(f"  {t['plays']:4d} plays  {t['pct']:5.1f}%  {t['artist']} — {t['title']}")

print("\n=== CONFIG ===")
for k, v in d['config'].items():
    print(f"  {k}: {v}")

# Test HTML endpoint responds
r2 = urllib.request.urlopen(f"{BASE}/admin")
html = r2.read()
print(f"\n/admin HTML: {len(html)} bytes, status 200 ✓")
