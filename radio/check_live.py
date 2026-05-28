import sqlite3
con = sqlite3.connect(r'C:\Dev\DeeJAI\radio\plays.db')
rows = con.execute(
    "SELECT artist, title, completed, context FROM plays "
    "WHERE track_path LIKE 'spotify:spotify:%' "
    "ORDER BY started_at DESC LIMIT 10"
).fetchall()
if rows:
    for r in rows:
        print(r)
else:
    print("No live rows yet — bridge hasn't fired or better-sqlite3 failed to load")
con.close()
