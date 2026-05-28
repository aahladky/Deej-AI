import urllib.request, json

BASE = "http://localhost:8050"

def get(url):
    r = urllib.request.urlopen(url)
    return json.loads(r.read())

def post(path, body):
    data = json.dumps(body).encode()
    req  = urllib.request.Request(f"{BASE}{path}", data=data,
                                   headers={"Content-Type": "application/json"})
    r = urllib.request.urlopen(req)
    return json.loads(r.read())

# 1. Health
h = get(f"{BASE}/api/health")
print("HEALTH:", h)

# 2. Home (filtered suggested should only have seedable tracks)
home = get(f"{BASE}/api/home")
print("\nHOME context:", home["context"])
print("Top artists:", home["top_artists"][:3])
print("Suggested (first 3):", home["suggested"][:3])

# 3. Recommend
rec = get(f"{BASE}/api/recommend?artist=Cody+Johnson&title=On+My+Way+to+You&n=5")
print("\nRECOMMEND seed:", rec["seed"])
print(f"Context: {rec['context']}  exploring: {rec['exploring']}")
for t in rec["tracks"]:
    print(f"  {t['score']:.4f}  {t['artist']} — {t['title']}")

# 4. Played (record a completion)
p = post("/api/played", {
    "artist":    "Cody Johnson",
    "title":     "On My Way to You",
    "album":     "Leather",
    "ms_played": 180000,
    "completed": True,
    "context":   "home"
})
print("\nPLAYED:", p)

# 5. Played (record a skip)
p2 = post("/api/played", {
    "artist":    "Alan Jackson",
    "title":     "Right Where I Want You",
    "ms_played": 30000,
    "completed": False
})
print("SKIPPED:", p2)
