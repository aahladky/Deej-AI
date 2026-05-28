import json
with open(r'C:\Dev\DeeJAI\radio\track_meta.json') as f:
    data = json.load(f)
keys = list(data.keys())[:2]
print('Total entries:', len(data))
print('Key sample:', keys[0])
print('Value sample:', data[keys[0]])
