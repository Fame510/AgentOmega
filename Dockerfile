FROM python:3.11-slim

WORKDIR /app

# System deps for Playwright chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install --with-deps chromium

COPY . .

# Audit ledger + session storage
RUN mkdir -p /app/data/sessions

EXPOSE 8000
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]
