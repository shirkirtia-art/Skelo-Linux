#!/usr/bin/env python3
"""
Skelo — list_windows.py

Step 1 of the Skelo Linux workflow: "what's open on screen right now."

Enumerates every application registered with the AT-SPI accessibility bus
and every top-level window (frame/dialog) each one owns.

Usage:
    python3 list_windows.py
    python3 list_windows.py --pretty=false
    python3 list_windows.py --all          # disable dedup/noise filtering

Output: JSON to stdout, e.g.

{
  "timestamp": "2026-07-10T10:52:14Z",
  "screen_resolution": {"width": 1920, "height": 1080},
  "window_count": 2,
  "windows": [
    {
      "app_name": "Spotify",
      "process_id": 4821,
      "window_title": "Spotify Premium",
      "role": "frame",
      "is_focused": true,
      "bounds": {"x": 0, "y": 0, "width": 1200, "height": 800}
    }
  ]
}

An agent should pick the window it cares about from this list, then call:
    python3 inspect_window.py --app "<app_name>" --title "<window_title>"

Requires: python3-gi, gir1.2-atspi-2.0, at-spi2-core, and accessibility
enabled (gsettings set org.gnome.desktop.interface toolkit-accessibility
true). If this prints an "error" key instead of window data, read its
"fix" field — that's almost always what's missing.
"""

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skelo_common import (
    ATSPI_AVAILABLE, atspi_unavailable_error,
    TOP_LEVEL_ROLES, get_bounds, is_state, get_screen_resolution,
    bounds_contained, dedupe, filter_nested_subframes,
)

if ATSPI_AVAILABLE:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
else:
    print(json.dumps(atspi_unavailable_error()))
    sys.exit(1)


def _collect_raw_windows_once():
    """A single, non-retrying pass. Returns (windows, err). err is only set
    for a hard failure (desktop bus unreachable, or the top-level call
    itself throwing) — per-app/per-window problems are swallowed so one
    flaky app doesn't blank out everything else."""
    windows = []
    try:
        desktop = Atspi.get_desktop(0)
    except Exception as e:
        return windows, str(e)

    try:
        app_count = desktop.get_child_count() if desktop else 0
    except Exception as e:
        return windows, str(e)

    for i in range(app_count):
        try:
            app = desktop.get_child_at_index(i)
        except Exception:
            # AT-SPI bus contention/races under rapid polling commonly throw
            # here transiently. Skip this one app rather than aborting the
            # whole scan.
            continue
        if app is None:
            continue

        app_name = app.get_name() or "unknown"
        try:
            pid = app.get_process_id()
        except Exception:
            pid = None

        try:
            win_count = app.get_child_count()
        except Exception:
            win_count = 0

        for j in range(win_count):
            try:
                win = app.get_child_at_index(j)
            except Exception:
                continue
            if win is None:
                continue

            try:
                role = win.get_role_name()
            except Exception:
                role = None

            if role not in TOP_LEVEL_ROLES:
                continue

            windows.append({
                "app_name": app_name,
                "process_id": pid,
                "window_title": win.get_name() or "",
                "role": role,
                "is_focused": is_state(win, Atspi.StateType.ACTIVE),
                "bounds": get_bounds(win),
                "source": "atspi",
            })

    return windows, None


def collect_raw_windows(retries=2, retry_delay=0.35):
    """Retries the whole AT-SPI pass a couple of times before giving up.
    This is the fix for the 'warning + error' behavior an agent sees when
    calling list_windows.py back-to-back rapidly: the AT-SPI session bus is
    a single shared resource, and hammering it produces the occasional
    transient failure (desktop temporarily unreachable, a window closing
    mid-enumeration). A blank/failed scan used to be surfaced immediately;
    now it's treated as recoverable and retried with a short backoff before
    it's reported as a real error."""
    last_err = None
    for attempt in range(retries + 1):
        windows, err = _collect_raw_windows_once()
        if err is None:
            return windows, None
        last_err = err
        if attempt < retries:
            time.sleep(retry_delay)
    return [], last_err


def get_x11_windows():
    """Gets all graphical windows currently open on X11 via wmctrl and xwininfo."""
    windows = []
    try:
        env = os.environ.copy()
        if "DISPLAY" not in env:
            env["DISPLAY"] = ":0"
            
        res = subprocess.run(["wmctrl", "-l", "-p"], capture_output=True, text=True, env=env, timeout=3)
        if res.returncode != 0:
            return []
            
        for line in res.stdout.strip().split("\n"):
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            win_id, workspace, pid_str, machine, title = parts
            pid = int(pid_str)
            if pid == 0:
                continue
                
            # Get geometry from xwininfo
            try:
                x_res = subprocess.run(["xwininfo", "-id", win_id], capture_output=True, text=True, env=env, timeout=2)
            except subprocess.TimeoutExpired:
                continue
            bounds = None
            if x_res.returncode == 0:
                x, y, w, h = None, None, None, None
                for wl in x_res.stdout.split("\n"):
                    wl = wl.strip()
                    if wl.startswith("Absolute upper-left X:"):
                        x = int(wl.split(":")[-1].strip())
                    elif wl.startswith("Absolute upper-left Y:"):
                        y = int(wl.split(":")[-1].strip())
                    elif wl.startswith("Width:"):
                        w = int(wl.split(":")[-1].strip())
                    elif wl.startswith("Height:"):
                        h = int(wl.split(":")[-1].strip())
                if x is not None and y is not None and w is not None and h is not None:
                    bounds = {"x": x, "y": y, "width": w, "height": h}
                    
            # Get app name
            app_name = "unknown"
            try:
                with open(f"/proc/{pid}/comm", "r") as f:
                    app_name = f.read().strip()
            except Exception:
                try:
                    c_res = subprocess.run(["xprop", "-id", win_id, "WM_CLASS"], capture_output=True, text=True, env=env, timeout=2)
                except subprocess.TimeoutExpired:
                    c_res = None
                if c_res is not None and c_res.returncode == 0 and "=" in c_res.stdout:
                    app_name = c_res.stdout.split("=")[-1].split(",")[0].strip('" \n')
                    
            raw_comm_name = app_name
            if app_name.lower() == "spotify":
                app_name = "Spotify"
            elif app_name.lower() == "chrome" or "chrome" in app_name.lower():
                app_name = "Google Chrome"
            elif app_name.lower() == "nemo":
                app_name = "nemo"
                
            windows.append({
                "app_name": app_name,
                "process_id": pid,
                "window_title": title,
                "role": "frame",
                "is_focused": False,
                "bounds": bounds,
                # Lower confidence than an AT-SPI-sourced entry: this app_name
                # came from /proc/<pid>/comm or WM_CLASS plus a hardcoded
                # name-guess table, not from the app's own accessibility
                # metadata. That heuristic is what previously misidentified
                # Spotify's CEF wrapper process as "Chromium" playing an
                # unrelated video. Kept here (rather than dropped) since for
                # many apps it's still the only signal available, but
                # callers doing identity-sensitive matching (e.g.
                # map_app.py's confirm_target_window) should treat it with
                # more scrutiny than an "atspi"-sourced entry.
                "source": "x11_fallback",
                "raw_comm_name": raw_comm_name,
            })
    except Exception:
        pass
    return windows


CACHE_FILE = "/tmp/.skelo_list_windows_cache.json"


def _read_cache(ttl, include_all):
    if ttl <= 0:
        return None
    try:
        st = os.stat(CACHE_FILE)
        age = time.time() - st.st_mtime
        if age > ttl:
            return None
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
        if data.get("_include_all") != include_all:
            return None
        data["cached"] = True
        data["cache_age_seconds"] = round(age, 3)
        data.pop("_include_all", None)
        return data
    except Exception:
        return None


def _write_cache(result, include_all):
    try:
        payload = dict(result)
        payload["_include_all"] = include_all
        with open(CACHE_FILE, "w") as f:
            json.dump(payload, f)
    except Exception:
        pass


def list_windows(include_all=False):
    raw, err = collect_raw_windows()
    if err:
        raw = []
        warning = f"Desktop enumeration issue: {err}"
    else:
        warning = None

    windows = dedupe(raw)
    if not include_all:
        windows = filter_nested_subframes(windows)

    # Merge X11 windows (such as Spotify or other apps without accessibility tree)
    x11_windows = get_x11_windows()
    seen_pids = {w["process_id"] for w in windows if w.get("process_id") is not None}
    
    for xw in x11_windows:
        if xw["process_id"] not in seen_pids:
            windows.append({
                "app_name": xw["app_name"],
                "process_id": xw["process_id"],
                "window_title": xw["window_title"],
                "role": xw["role"],
                "is_focused": xw["is_focused"],
                "bounds": xw["bounds"],
                "source": xw.get("source", "x11_fallback"),
                "raw_comm_name": xw.get("raw_comm_name"),
            })
            seen_pids.add(xw["process_id"])

    return windows, warning


def main():
    parser = argparse.ArgumentParser(description="List all open windows via AT-SPI")
    parser.add_argument("--pretty", default="true", help="Pretty-print JSON (true/false)")
    parser.add_argument(
        "--all", action="store_true",
        help="Disable dedup/noise filtering; show every raw AT-SPI top-level accessible",
    )
    parser.add_argument(
        "--cache-ttl", type=float, default=0.75,
        help="Reuse a scan result younger than this many seconds instead of "
             "re-querying AT-SPI/X11 (protects the bus from rapid repeated "
             "calls). Set to 0 to always do a fresh scan.",
    )
    args = parser.parse_args()

    cached = _read_cache(args.cache_ttl, args.all)
    if cached is not None:
        indent = 2 if args.pretty.lower() != "false" else None
        print(json.dumps(cached, indent=indent))
        return

    windows, err = list_windows(include_all=args.all)

    result = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "screen_resolution": get_screen_resolution(),
        "window_count": len(windows),
        "windows": windows,
    }
    if err:
        result["warning"] = f"Desktop enumeration issue: {err}"
    else:
        _write_cache(result, args.all)

    indent = 2 if args.pretty.lower() != "false" else None
    print(json.dumps(result, indent=indent))


if __name__ == "__main__":
    main()