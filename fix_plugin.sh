#!/bin/sh
# One-shot manual repair of the bgutil plugin (run inside the bot's venv).
set -e
pip uninstall -y bgutil-ytdlp-pot-provider || true
SP=$(python -c "import site;print(site.getsitepackages()[0])")
rm -rf "$SP/yt_dlp_plugins"
pip install --no-cache-dir -U "yt-dlp[default]"
pip install --no-cache-dir --force-reinstall --no-deps bgutil-ytdlp-pot-provider==1.3.1
python -c "import yt_dlp_plugins.extractor.getpot_bgutil_http" && echo "plugin OK"
