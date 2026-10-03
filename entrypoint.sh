#!/bin/sh
# Starts the bgutil PO-token server (built into the image, see Dockerfile) on
# loopback only, waits for it, then hands over to the bot. If it doesn't come
# up the bot still runs — YouTube just uses the cookie-free client sets.
#
# The 1.3.1 server has no --host option, so force_loopback.cjs is preloaded to pin
# it to 127.0.0.1. If the server won't start with that preload, retry once without it
# (better a working PO token than none; keep port 4416 unpublished in that case).
MAIN=/opt/bgutil-pot/server/build/main.js
PRELOAD=/app/force_loopback.cjs

start_pot() {
    if [ "$1" = "preload" ] && [ -f "$PRELOAD" ]; then
        node --require "$PRELOAD" "$MAIN" --port 4416 > /tmp/bgutil-pot.log 2>&1 &
    else
        node "$MAIN" --port 4416 > /tmp/bgutil-pot.log 2>&1 &
    fi
    BGUTIL_PID=$!
    UP=0
    for i in $(seq 1 30); do
        if curl -fsS http://127.0.0.1:4416/ping >/dev/null 2>&1; then UP=1; break; fi
        kill -0 "$BGUTIL_PID" 2>/dev/null || break
        sleep 1
    done
}

if [ -f "$MAIN" ]; then
    start_pot preload
    if [ "$UP" != "1" ]; then
        echo "[bgutil-pot] WARNING — did not come up with loopback preload, retrying without it"
        kill "$BGUTIL_PID" 2>/dev/null
        sleep 1
        start_pot plain
    fi
    if [ "$UP" = "1" ]; then
        echo "[bgutil-pot] OK — PO token server is up on 127.0.0.1:4416"
    else
        echo "[bgutil-pot] WARNING — PO token server did not come up. Last log lines:"
        tail -n 20 /tmp/bgutil-pot.log 2>/dev/null
    fi
fi
exec python3 bot.py
