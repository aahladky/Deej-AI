import urllib.request, json

r = urllib.request.urlopen("http://localhost:8050/api/health")
print("health:", json.loads(r.read()))

# Try admin stats directly
try:
    r2 = urllib.request.urlopen("http://localhost:8050/api/admin/stats")
    print("admin/stats OK:", len(r2.read()), "bytes")
except Exception as e:
    print("admin/stats error:", e)

# Try admin html
try:
    r3 = urllib.request.urlopen("http://localhost:8050/admin")
    print("admin HTML OK:", len(r3.read()), "bytes")
except Exception as e:
    print("admin HTML error:", e)
