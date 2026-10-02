#!/bin/sh
# Starts the bgutil PO-token server (built into the image, see Dockerfile) on
# loopback only, waits for it, then hands over to the bot. If it doesn't come
# up the bot still runs — YouTube just uses the cookie-free client sets.
if [ -f /opt/bgutil-pot/server/build/main.js ]; then
    node /opt/bgutil-pot/server/build/main.js --host 127.0.0.1 > /tmp/bgutil-pot.log 2>&1 &
    BGUTIL_PID=$!
    UP=0
    for i in $(seq 1 30); do
        if curl -fsS http://127.0.0.1:4416/ping >/dev/null 2>&1; then UP=1; break; fi
        kill -0 "$BGUTIL_PID" 2>/dev/null || break
        sleep 1
    done
    if [ "$UP" = "1" ]; then
        echo "[bgutil-pot] OK — PO token server is up on 127.0.0.1:4416"
    else
        echo "[bgutil-pot] WARNING — PO token server did not come up. Last log lines:"
        tail -n 20 /tmp/bgutil-pot.log 2>/dev/null
    fi
fi
exec python3 bot.py
