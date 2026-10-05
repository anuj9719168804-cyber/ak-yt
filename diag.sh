#!/bin/sh
# Diagnose "Only images are available" — run inside the bot's venv:  sh diag.sh [youtube-url]
# Paste the WHOLE output back.
URL="${1:-https://youtu.be/33PyGgLmshk}"
echo "### versions"; yt-dlp --version; pip list 2>/dev/null | grep -i -E "yt-dlp|bgutil|curl|ejs"; node -v; python -V
echo "### pot server"; curl -s -m 3 http://127.0.0.1:4416/ping || echo "pot server NOT answering"
echo; echo "### plugins seen by yt-dlp"; yt-dlp -v --simulate "$URL" 2>&1 | grep -i -E "plugin|PO Token Providers|js runtime|ejs" | head -12
for c in mweb web_safari web_embedded android_vr tv default; do
  echo; echo "################ client: $c"
  yt-dlp -F --no-warnings --extractor-args "youtube:player_client=$c" "$URL" 2>&1 | tail -n 12
done
