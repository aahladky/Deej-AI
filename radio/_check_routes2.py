import sys
sys.path.insert(0, r'C:\Dev\DeeJAI\radio')
# Monkey-patch _startup so it doesn't load files
import deejay_api as api
print("Registered routes:")
for rule in sorted(str(r) for r in api.app.url_map.iter_rules()):
    print(f"  {rule}")
