    : > "$LOG"
    # shellcheck disable=SC2086
    node "$MAIN" $1 >> "$LOG" 2>&1 &
    BGUTIL_PID=$!
    for i in $(seq 1 30); do
        if curl -fsS http://127.0.0.1:4416/ping >/dev/null 2>&1; then return 0; fi
        kill -0 "$BGUTIL_PID" 2>/dev/null || return 1
        sleep 1
    done
    return 1
}

if [ -f "$MAIN" ]; then
    echo "[bgutil-pot] node $(node -v)"
    if try_start "--host 127.0.0.1"; then
        echo "[bgutil-pot] OK — PO token server is up on 127.0.0.1:4416"
    else
        echo "[bgutil-pot] start with --host failed, log:"; tail -n 30 "$LOG"
        kill "$BGUTIL_PID" 2>/dev/null
        # some server versions don't know --host (exit code 1) — retry with defaults
        if try_start ""; then
            echo "[bgutil-pot] OK (without --host) — PO token server is up on :4416"
        else
            echo "[bgutil-pot] WARNING — PO token server did not come up. Last log lines:"
            tail -n 30 "$LOG"
        fi
    fi
fi
exec python3 bot.py
