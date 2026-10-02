"""
YouTube PO-token provider (Brainicism/bgutil-ytdlp-pot-provider), ported from fbot.

yt-dlp's "web" YouTube client is the one that exposes the full quality ladder
(1080p / 1440p / 4K), but YouTube only serves those formats when a valid
"proof-of-origin" token is attached. The pip plugin `bgutil-ytdlp-pot-provider`
(see requirements.txt) attaches it automatically by talking to a small local
Node.js HTTP server on 127.0.0.1:4416. This module makes sure that server is
running.

Docker deploy : the Dockerfile builds the server into /opt/bgutil-pot at image
                build time and entrypoint.sh starts it before the bot, so
                start_background() just finds it already listening and returns.
Plain `python bot.py` deploy : needs git + node + npm on PATH. The server is
                cloned and built (npm ci && npx tsc) into ./.bgutil-pot on first
                boot, in a background thread, so bot startup is never delayed.

Every step is wrapped: if anything fails the bot keeps working exactly as before
(is_ready() stays False, bot.py keeps using its cookie-free client sets).
`/potstatus` (admin) shows get_status() — the exact step that failed.
"""
import logging
import os
import shutil
import socket
import subprocess
import threading
import time

logger = logging.getLogger("ytbot")

POT_VERSION = "1.3.1"
POT_REPO_URL = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"
POT_PORT = 4416

_HERE = os.path.dirname(os.path.abspath(__file__))
POT_HOME = os.path.join(_HERE, ".bgutil-pot")
# Prebuilt by the Dockerfile — preferred when present.
DOCKER_MAIN_JS = "/opt/bgutil-pot/server/build/main.js"
LOCAL_SERVER_DIR = os.path.join(POT_HOME, "server")
LOCAL_MAIN_JS = os.path.join(LOCAL_SERVER_DIR, "build", "main.js")

_ready = threading.Event()
_server_process = None
_status = "not started"
_status_lock = threading.Lock()


def _set_status(s: str):
    global _status
    with _status_lock:
        _status = s
    logger.info(f"[pot-provider] {s}")


def is_ready() -> bool:
    """True once the PO-token server is accepting connections on 127.0.0.1:4416."""
    return _ready.is_set()


def get_status() -> str:
    with _status_lock:
        return _status


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _run(cmd, cwd=None, timeout=300) -> bool:
    try:
        r = subprocess.run(cmd, cwd=cwd, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if r.returncode != 0:
            out = (r.stdout or b"").decode("utf-8", errors="replace")[-2000:]
            logger.warning(f"[pot-provider] command failed ({' '.join(cmd)}): {out}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning(f"[pot-provider] command timed out ({timeout}s): {' '.join(cmd)}")
        return False
    except Exception as e:
        logger.warning(f"[pot-provider] command errored ({' '.join(cmd)}): {e}")
        return False


def _start_server(main_js: str, cwd: str) -> bool:
    global _server_process
    _set_status(f"starting provider HTTP server on port {POT_PORT}...")
    last_out = ""
    # First with --host 127.0.0.1 (loopback only), then without it: some server
    # versions reject the flag and exit with code 1.
    for extra in (["--host", "127.0.0.1"], []):
        log_path = os.path.join(_HERE, ".bgutil-pot-server.log")
        try:
            logf = open(log_path, "wb")
            _server_process = subprocess.Popen(
                ["node", main_js] + extra, cwd=cwd, stdout=logf, stderr=subprocess.STDOUT,
            )
        except Exception as e:
            _set_status(f"failed: couldn't start the server process: {e}")
            return False
        for _ in range(20):
            if _port_open("127.0.0.1", POT_PORT):
                _ready.set()
                _set_status("ready — PO-token provider is up, full YouTube quality ladder available.")
                return True
            if _server_process.poll() is not None:
                break
            time.sleep(1)
        try:
            with open(log_path, "rb") as f:
                last_out = f.read()[-600:].decode("utf-8", errors="replace").strip()
        except OSError:
            pass
        if _server_process.poll() is None:
            _server_process.kill()
    _set_status(f"failed: server exited/never listened. node output: {last_out or '(empty)'}")
    return False


def _setup_and_start():
    # 1. Already running (entrypoint.sh started it, or a previous boot).
    if _port_open("127.0.0.1", POT_PORT):
        _ready.set()
        _set_status("ready — server already up (started by entrypoint.sh).")
        return

    if not shutil.which("node"):
        _set_status("failed: node not found on PATH — PO-token provider skipped, "
                    "YouTube stays on the cookie-free client sets.")
        return

    # 2. Docker image has it prebuilt but nothing started it — just launch it.
    if os.path.exists(DOCKER_MAIN_JS):
        _start_server(DOCKER_MAIN_JS, os.path.dirname(os.path.dirname(DOCKER_MAIN_JS)))
        return

    # 3. Plain deploy: clone + build once, then launch.
    _set_status("checking for git/npm...")
    missing = [t for t in ("git", "npm") if not shutil.which(t)]
    if missing:
        _set_status(f"failed: {', '.join(missing)} not found on PATH — PO-token provider skipped.")
        return

    if not os.path.isdir(os.path.join(LOCAL_SERVER_DIR, "src")):
        _set_status(f"cloning provider server ({POT_VERSION})...")
        shutil.rmtree(POT_HOME, ignore_errors=True)
        if not _run(["git", "clone", "--single-branch", "--branch", POT_VERSION,
                     "--depth", "1", POT_REPO_URL, POT_HOME], timeout=90):
            _set_status("failed: git clone of the provider repo didn't succeed.")
            return

    if not os.path.exists(LOCAL_MAIN_JS):
        _set_status("building provider server (npm ci && npx tsc) — first boot only...")
        if not _run(["npm", "ci"], cwd=LOCAL_SERVER_DIR, timeout=300):
            _set_status("failed: npm ci didn't succeed.")
            return
        if not _run(["npx", "tsc"], cwd=LOCAL_SERVER_DIR, timeout=180):
            _set_status("failed: npx tsc (build) didn't succeed.")
            return

    _start_server(LOCAL_MAIN_JS, LOCAL_SERVER_DIR)


def start_background():
    """Call once at startup. Non-blocking; safe on every platform."""
    threading.Thread(target=_setup_and_start, name="pot-provider-setup", daemon=True).start()
