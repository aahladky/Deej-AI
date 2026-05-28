FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer-cached until requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY deejay_api.py scorer.py dj_session.py track_meta.json ./

# Data paths are mounted at runtime — nothing baked into the image
# /data/music       → E:\Media\Music  (read-only)
# /data/pickles     → C:\Dev\DeeJAI\pickles  (read-only)
# /data/radio       → C:\Dev\DeeJAI\radio    (rw — plays.db lives here)

ENV PYTHONUNBUFFERED=1

EXPOSE 8050

CMD ["python", "deejay_api.py"]
