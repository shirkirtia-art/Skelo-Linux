#!/usr/bin/env python3
"""
Skelo — skelo_resolve.py

Loads an App Skill Profile (produced by map_app.py), queries the current
live window position/size, dynamically computes the resized click target
for a named button using the profile's layout anchoring, and outputs the
coordinates or executes a click immediately.

Use this for apps you've already mapped once with map_app.py, when the
window may since have moved or been resized — it recalculates the click
target from the *current* geometry rather than replaying a stale absolute
coordinate. For everything else (an app you haven't mapped, or one where
AT-SPI labels are enough), skelo_action.py is the better default: it
doesn't need a prior mapping pass and re-resolves live every time anyway.

Usage:
    # Resolve coordinates for the Settings button in OBS Studio
    python3 skelo_resolve.py --skill skills/obs.json --label "Settings"

    # Click the Settings button in OBS Studio immediately
    python3 skelo_resolve.py --skill skills/obs.json --label "Settings" --click
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skelo_common import ATSPI_AVAILABLE, atspi_unavailable_error, find_window, get_bounds

if not ATSPI_AVAILABLE:
    print(json.dumps(atspi_unavailable_error()))
    sys.exit(1)


def find_live_window_bounds(app_name, title_query=None):
    """Finds the app's window and returns its current screen bounds
    (thin wrapper around the shared find_window(), which returns the
    accessible objects rather than a bounds dict)."""
    _, win = find_window(app_name, title_query)
    if win is None:
        return None
    bounds = get_bounds(win)
    if not bounds or bounds["width"] <= 0 or bounds["height"] <= 0:
        return None
    bounds["title"] = win.get_name() or ""
    return bounds


def resolve_coords(control, cur_bounds):
    """Calculates the adjusted click target coordinates on the resized window."""
    win_x = cur_bounds["x"]
    win_y = cur_bounds["y"]
    win_w = cur_bounds["width"]
    win_h = cur_bounds["height"]

    anchor = control.get("anchor_style", "proportional")
    meta = control["anchor_meta"]

    rel_cx = control["relative_click_target"]["x"]
    rel_cy = control["relative_click_target"]["y"]

    dist_r = meta["dist_from_right"]
    dist_b = meta["dist_from_bottom"]
    ratio_x = meta["ratio_x"]
    ratio_y = meta["ratio_y"]

    if anchor == "top-left":
        dx, dy = rel_cx, rel_cy
    elif anchor == "top-right":
        dx, dy = win_w - dist_r, rel_cy
    elif anchor == "bottom-left":
        dx, dy = rel_cx, win_h - dist_b
    elif anchor == "bottom-right":
        dx, dy = win_w - dist_r, win_h - dist_b
    elif anchor == "bottom-center":
        dx, dy = ratio_x * win_w, win_h - dist_b
    elif anchor == "top-center":
        dx, dy = ratio_x * win_w, rel_cy
    else:  # proportional
        dx, dy = ratio_x * win_w, ratio_y * win_h

    return {"x": win_x + dx, "y": win_y + dy}


def main():
    parser = argparse.ArgumentParser(
        description="Resolves coordinates dynamically for resized/moved application windows using Skelo App Profiles."
    )
    parser.add_argument("--skill", required=True, help="Path to the JSON app profile (e.g. skills/obs.json).")
    parser.add_argument("--label", required=True, help="Sub-string match for the target control button.")
    parser.add_argument("--click", action="store_true", help="Execute a click immediately on the resolved coordinates.")
    parser.add_argument("--method", default="cursor", choices=["cursor", "action"],
                         help="'cursor' (default) simulates a mouse click via skelo_action.py; "
                              "'action' invokes the element's AT-SPI action directly (needs --confirm-live-lookup).")
    args = parser.parse_args()

    if not os.path.exists(args.skill):
        print(json.dumps({"status": "error", "error": f"Skill profile file '{args.skill}' not found."}))
        sys.exit(1)

    try:
        with open(args.skill, "r") as f:
            profile = json.load(f)
    except Exception as e:
        print(json.dumps({"status": "error", "error": f"Failed to load JSON profile: {e}"}))
        sys.exit(1)

    app_name = profile["app_name"]
    ref_bounds = profile["window_bounds"]
    controls = profile["controls"]

    matched_controls = [c for c in controls if args.label.lower() in c["label"].lower()]
    if not matched_controls:
        print(json.dumps({
            "status": "error",
            "error": f"No control button matching '{args.label}' in skill profile.",
            "available_labels": [c["label"] for c in controls],
        }))
        sys.exit(1)

    exact_matches = [c for c in matched_controls if c["label"].lower() == args.label.lower()]
    target_control = exact_matches[0] if exact_matches else matched_controls[0]

    # Catches two cases: profiles saved by a version of map_app.py new
    # enough to flag this explicitly (bounds_reliable: False), and older
    # profiles that predate the flag but still carry the raw AT-SPI
    # sentinel value directly in absolute_bounds. Either way, resolving
    # "coordinates" from this would silently compute a nonsense click
    # target and fail confusingly deep inside skelo_action.py rather than
    # here, with a clear reason, before any math happens on garbage input.
    _bounds = target_control.get("absolute_bounds") or {}
    _sentinel = target_control.get("bounds_reliable") is False or any(
        _bounds.get(k, 0) <= -1_000_000 for k in ("x", "y")
    )
    if _sentinel:
        print(json.dumps({
            "status": "error",
            "error": (
                f"Control '{target_control['label']}' has no reliable screen "
                "position to resolve. This is a known AT-SPI limitation — "
                "commonly a menu item that only gets valid coordinates while "
                "its containing menu is actually open, not while it's saved "
                "in a learned profile."
            ),
            "hint": (
                "Open the containing menu first (e.g. click the parent menu "
                "button), then interact with this control while it's actually "
                "visible — skelo_action.py's live --label lookup re-resolves "
                "against the current tree and doesn't have this problem, unlike "
                "a saved skill profile's coordinates."
            ),
        }))
        sys.exit(1)

    cur_bounds = find_live_window_bounds(app_name)
    if not cur_bounds:
        print(json.dumps({
            "status": "error",
            "error": f"Application '{app_name}' is not currently running or has no open window.",
        }))
        sys.exit(1)

    resolved = resolve_coords(target_control, cur_bounds)

    result = {
        "status": "success",
        "app_name": app_name,
        "control_label": target_control["label"],
        "anchor_style": target_control["anchor_style"],
        "learned_window_bounds": ref_bounds,
        "current_window_bounds": cur_bounds,
        "resolved_click_target": resolved,
    }

    if args.click:
        # BUG FIX: this used to pass --confirm to skelo_action.py, which has
        # no such flag — every click silently failed with an argparse error
        # that was swallowed into click_action.error. skelo_action.py is
        # coordinate-only and has no AT-SPI element to "confirm" against
        # anyway, so we just call it plainly and report its own status.
        script_dir = os.path.dirname(os.path.abspath(__file__))
        action_script = os.path.join(script_dir, "skelo_action.py")

        cmd = [
            sys.executable, action_script,
            "--action", "click",
            "--x", str(resolved["x"]),
            "--y", str(resolved["y"]),
        ]

        try:
            action_res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if action_res.returncode != 0:
                result["click_action"] = {
                    "status": "error",
                    "error": f"skelo_action.py exited {action_res.returncode}",
                    "stderr": action_res.stderr.strip(),
                }
            else:
                result["click_action"] = json.loads(action_res.stdout)
        except Exception as e:
            result["click_action"] = {"status": "error", "error": str(e)}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
