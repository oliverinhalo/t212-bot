# One image, two services (see docker-compose.yml): the trading bot and the
# dashboard each use it with a different command. Neither .env, config.yaml,
# nor data/ is baked in — all three are mounted at runtime, the same
# separation the systemd deploy (deploy/) already keeps.

FROM python:3.11-slim

WORKDIR /app

# tzdata: the scheduler and dashboard both format times in a configured
# timezone (schedule.timezone, e.g. Europe/London), not just UTC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Overridden per-service by docker-compose.yml's `command:`.
CMD ["python", "-m", "t212bot.dashboard"]
