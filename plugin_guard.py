"""
Self-heal for a corrupted bgutil PO-token plugin install.

Symptom (bot log at startup):
    ImportError: cannot import name 'BgUtilPTPBase' from 'yt_dlp_plugins.extractor.getpot_bgutil'
    AssertionError: PoTokenProvider BgUtilScriptNode already registered
Cause: yt_dlp_plugins/extractor/ holds files from two different plugin versions
(pip upgraded over leftovers). The PO-token provider then fails to load, so the
`web` / `web_safari` clients get no token and downloads fail.

check_and_repair() MUST run before anything imports yt_dlp. It only looks at files
(no yt_dlp import), and if the install is inconsistent it wipes the plugin dir,
reinstalls the pinned version and re-execs the bot once.
"""
import glob
import importlib.util
import os
import shutil
import subprocess
import sys

PLUGIN_PIN = "bgutil-ytdlp-pot-provider==1.3.1"
_ENV_FLAG = "AKYT_PLUGIN_REPAIRED"


def _plugin_dirs():
    dirs = []
    try:
        spec = importlib.util.find_spec("yt_dlp_plugins")
        if spec and spec.submodule_search_locations:
            dirs += list(spec.submodule_search_locations)
    except Exception:
        pass
    import site
    for sp in set(site.getsitepackages() + [site.getusersitepackages()]):
        d = os.path.join(sp, "yt_dlp_plugins")
        if os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs


def _is_broken(plugin_dir: str) -> bool:
    ext = os.path.join(plugin_dir, "extractor")
    base = os.path.join(ext, "getpot_bgutil.py")
    if not os.path.isfile(base):
        return False
    try:
        base_src = open(base, encoding="utf-8").read()
        for f in glob.glob(os.path.join(ext, "getpot_bgutil_*.py")):
            src = open(f, encoding="utf-8").read()
            # subclass file expects the base class that the base file doesn't define
            if "BgUtilPTPBase" in src and "class BgUtilPTPBase" not in base_src:
                return True
    except Exception:
        return False
    # stray duplicate provider registrations (e.g. a leftover renamed copy)
    names = [os.path.basename(f) for f in glob.glob(os.path.join(ext, "getpot_bgutil*.py"))]
    return len(names) != len(set(names))


def check_and_repair():
    if os.environ.get(_ENV_FLAG):
        return
    broken = [d for d in _plugin_dirs() if _is_broken(d)]
    if not broken:
        return
    print(f"[plugin-guard] broken bgutil plugin install in {broken} — repairing", flush=True)
    pip = [sys.executable, "-m", "pip"]
    # SAFETY: never wipe the plugin unless we can also put it back. Wiping first and failing to reinstall
    # (no write permission as the non-root "bot" user, no network) left the bot with NO PO-token plugin,
    # so every client lost its token and info fetching broke.
    unwritable = [d for d in broken if not os.access(d, os.W_OK)]
    if unwritable:
        print(f"[plugin-guard] {unwritable} not writable by this user — skipping repair "
              "(run plugin.sh as root / rebuild the image instead)", flush=True)
        return
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        if subprocess.call(pip + ["download", "--no-deps", "-q", "-d", tmp, PLUGIN_PIN]) != 0:
            print("[plugin-guard] cannot download the plugin (no network?) — skipping repair, nothing was removed",
                  flush=True)
            return
    subprocess.call(pip + ["uninstall", "-y", "bgutil-ytdlp-pot-provider"])
    for d in broken:
        shutil.rmtree(d, ignore_errors=True)
    rc = subprocess.call(pip + ["install", "--no-cache-dir", "--force-reinstall", "--no-deps", PLUGIN_PIN])
    if rc != 0:
        print("[plugin-guard] reinstall failed — run: pip install --no-cache-dir "
              f"--force-reinstall {PLUGIN_PIN}", flush=True)
        return
    os.environ[_ENV_FLAG] = "1"
    print("[plugin-guard] repaired — restarting", flush=True)
    os.execv(sys.executable, [sys.executable] + sys.argv)
