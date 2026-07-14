# ─── OptiBot Mini-Clone — Dockerfile ─────────────────────────────
# Daily cron job: scrape Zendesk → detect changes → upload delta to OpenAI
#
# Build:
#   docker build -t optibot-clone .
#
# Run (one-shot):
#   docker run --rm -e OPENAI_API_KEY=sk-... optibot-clone
#
# The container scrapes, uploads deltas, and exits 0.

FROM python:3.12-slim

# ─── System dependencies ─────────────────────────────────────────
# html2text + BeautifulSoup only need the Python libs, no system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ─── App setup ───────────────────────────────────────────────────
WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY scraper.py .
COPY uploader.py .
COPY main.py .
COPY articles/ ./articles/

# scrape_state.json may not exist on first build — that's fine,
# scraper.py handles the missing-file case (returns {})

# Run the pipeline once and exit
CMD ["python", "main.py"]
