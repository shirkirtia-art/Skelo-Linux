#!/usr/bin/env python3
"""
Skelo — skelo_learn.py

Single entry point for the "learn this application" workflow described in
SKILL.md. It exists because the 5-step manual flow (list_windows.py ->
app_launcher.py -> isolate -> map_app.py -> save) only works if every step
actually runs *in order*. An agent under time/token pressure will
sometimes skip straight to map_app.py on an app that was never confirmed
to be open, or on the wrong window — that used to silently map whatever
AT-SPI happened to resolve first and save it under the wrong name.

skelo_learn.py collapses the whole sequence into one call:

    1. list_windows.py  — is "<app>" already open?
    2. app_launcher.py  — if not, launch it (fails loudly if the app isn't
                           installed or the name is ambiguous)
    3. re-check          — confirm a window actually appeared post-launch
    4. raise/focus        — best-effort wmctrl raise of the target window
    5. map_app.py        — map_application(), which itself re-confirms
                           identity against the open-window list before
                           touching anything (see map_app.py)

Usage:
    python3 skelo_learn.py --app "OBS Studio"
    python3 skelo_learn.py --app "OBS Studio" --title "Main Window"
    python3 skelo_learn.py --app "OBS Studio" --skip-launch   # error out instead of auto-launching
"""

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from list_windows import list_windows
from app_launcher import get_apps_categorized, launch_app_by_style
from map_app import map_application


def find_open(app_query, title_query=None):
    windows, _warning = list_windows()
    q = app_query.lower()
    matches = [w for w in windows if q in (w.get("app_name") or "").lower()]
    if title_query:
        tq = title_query.lower()
        matches = [w for w in matches if tq in (w.get("window_title") or "").lower()]
    return matches


def launch_target(app_query):
    """Looks up app_query in the installed-application index and launches
    it, the same way app_launcher.py --open <name> would. Returns
    (launched_app_dict_or_None, error_or_None)."""
    indexed_apps, _categorized = get_apps_categorized()
    q = app_query.lower()

    exact = [a for a in indexed_apps.values() if a["name"].lower() == q]
    candidates = exact if exact else [a for a in indexed_apps.values() if q in a["name"].lower()]

    if not candidates:
        return None, (f"No installed application found matching '{app_query}'. "
                       f"Run app_launcher.py --list to see what's available.")
    if len(candidates) > 1:
        names = sorted({a["name"] for a in candidates})
        return None, f"Ambiguous app name '{app_query}'. Candidates: {names}. Be more specific."

    ok, err = launch_app_by_style(candidates[0])
    return (candidates[0] if ok else None), err


def run(app_query, title_query=None, skip_launch=False, launch_wait=4.0, output=None):
    steps = []

    matches = find_open(app_query, title_query)
    steps.append({"step": "check_open", "matches_found": len(matches)})

    if not matches:
        if skip_launch:
            return {
                "status": "error",
                "error": f"'{app_query}' is not currently open and --skip-launch was set.",
                "steps": steps,
            }

        launched, err = launch_target(app_query)
        steps.append({"step": "launch", "ok": launched is not None, "error": err})
        if launched is None:
            return {"status": "error", "error": err, "steps": steps}

        time.sleep(launch_wait)
        matches = find_open(app_query, title_query)
        steps.append({"step": "recheck_open", "matches_found": len(matches)})

        if not matches:
            return {
                "status": "error",
                "error": f"Launched '{app_query}' but no matching window appeared "
                         f"within {launch_wait}s.",
                "hint": "The app may need more time to start, or its window title/"
                        "app name differs from what was launched. Try list_windows.py "
                        "manually, or increase --launch-wait.",
                "steps": steps,
            }

    # Best-effort raise/focus of the target before mapping. This is not a
    # substitute for map_application()'s own identity confirmation — it's
    # just trying to get the right window on top so test-clicks land where
    # expected.
    target = matches[0]
    try:
        subprocess.run(
            ["wmctrl", "-a", target.get("window_title") or target.get("app_name") or app_query],
            timeout=2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    steps.append({"step": "raise_window", "target_app": target.get("app_name"),
                  "target_title": target.get("window_title")})

    result = map_application(app_query, title_query, output)
    result["steps"] = steps
    return result


def main():
    parser = argparse.ArgumentParser(
        description="End-to-end Skelo learning flow: confirm/launch the app, then map it."
    )
    parser.add_argument("--app", required=True, help="App name substring (case-insensitive)")
    parser.add_argument("--title", default=None, help="Window title substring (case-insensitive)")
    parser.add_argument("--skip-launch", action="store_true", dest="skip_launch",
                         help="Fail instead of auto-launching if the app isn't already open.")
    parser.add_argument("--launch-wait", type=float, default=4.0, dest="launch_wait",
                         help="Seconds to wait after launching before re-checking for a window.")
    parser.add_argument("--output", default=None, help="Custom output JSON path for the skill profile.")
    args = parser.parse_args()

    result = run(args.app, args.title, args.skip_launch, args.launch_wait, args.output)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") == "success" else 1)


if __name__ == "__main__":
    main()