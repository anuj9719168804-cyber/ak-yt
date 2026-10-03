FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc libffi-dev python3-dev ffmpeg curl git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp PO-token server (bgutil-ytdlp-pot-provider). Built once at image build
# time; entrypoint.sh starts it. The pip plugin that talks to it is pinned to the
# SAME version in requirements.txt (a server/plugin version mismatch = no tokens).
ENV BGUTIL_POT_VERSION=1.3.1
RUN git clone --single-branch --branch ${BGUTIL_POT_VERSION} --depth 1 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-pot \
    && cd /opt/bgutil-pot/server \
    && npm ci \
    && npx tsc

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -U "yt-dlp[default]"

COPY . .
RUN chmod +x /app/entrypoint.sh \
    && useradd --create-home --uid 10001 bot \
    && chown -R bot:bot /app /opt/bgutil-pot

# Run as an unprivileged user. If you mount a volume for DATA_FILE / cookies,
# make sure it is writable by uid 10001.
USER bot

# PID 1 is `python3 bot.py` (entrypoint execs it): unhealthy if the bot is gone.
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD grep -qa "bot.py" /proc/1/cmdline || exit 1

CMD ["/app/entrypoint.sh"]
