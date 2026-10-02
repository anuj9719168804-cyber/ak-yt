FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc libffi-dev python3-dev ffmpeg curl git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp PO-token server (bgutil-ytdlp-pot-provider). Built once at image build
# time; entrypoint.sh starts it. The pip plugin that talks to it is in
# requirements.txt. Pinned to a release tag, not the moving default branch.
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
RUN chmod +x /app/entrypoint.sh
CMD ["/app/entrypoint.sh"]
