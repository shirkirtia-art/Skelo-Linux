#!/usr/bin/env python3
"""
Skelo — map_app.py

Discovers and maps an application's layout.
Clicks each interactive control, analyzes the result (e.g., dialogs opened,
popup menus spawned, or label changes), closes any popped-up windows/menus,
and writes the detailed profile map to the skills/ directory.

Usage:
    python3 map_app.py --app "obs"
"""

import argparse
import json
import os
import sys
import time
import warnings
import subprocess

# Suppress deprecation warnings from PyGObject/AT-SPI
warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
except (ImportError, ValueError) as e:
    print(json.dumps({
        "status": "error",
        "error": "AT-SPI GObject bindings not available",
        "detail": str(e)
    }))
    sys.exit(1)

try:
    import pyautogui
    pyautogui.FAILSAFE = False
except ImportError:
    print(json.dumps({
        "status": "error",
        "error": "pyautogui not available"
    }))
    sys.exit(1)

# Include skelo path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skelo_common import find_window, get_bounds, resolve_label, classify_region, ROLE_TYPE_MAP, try_activate_window
from list_windows import list_windows as scan_open_windows

# Anchored to this script's own location, not the caller's cwd — an agent
# invoking map_app.py from different working directories used to end up
# with skill profiles scattered across whatever "skills/" happened to
# resolve to relative to wherever it was standing at the time.
_SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")


def determine_anchor(region):
    if region == "top-left" or region == "middle-left":
        return "top-left"
    elif region == "top-right" or region == "middle-right":
        return "top-right"
    elif region == "bottom-left":
        return "bottom-left"
    elif region == "bottom-right":
        return "bottom-right"
    elif region == "bottom-center":
        return "bottom-center"
    elif region == "top-center":
        return "top-center"
    else:
        return "proportional"


class AppMapper:
    def __init__(self, max_depth=30, max_elements=2000):
        self.max_depth = max_depth
        self.max_elements = max_elements
        self.visited = 0
        self.controls = []

    def walk(self, acc, depth, win_bounds):
        if acc is None or depth > self.max_depth or self.visited >= self.max_elements:
            return

        self.visited += 1

        try:
            role = acc.get_role_name() or "unknown"
        except Exception:
            role = "unknown"

        elem_type = ROLE_TYPE_MAP.get(role, role.replace(" ", "_") if role else "unknown")
        label, label_inferred = resolve_label(acc, role)
        bounds = get_bounds(acc)

        try:
            states = acc.get_state_set()
            visible = (states.contains(Atspi.StateType.SHOWING) or states.contains(Atspi.StateType.VISIBLE)) if states else True
        except Exception:
            visible = True

        # Map interactive controls
        is_interactive_type = elem_type in (
            "button", "toggle_button", "text_field", "checkbox", "radio_button",
            "combo_box", "list_item", "slider", "tab", "menu_item",
            "link", "icon", "table_cell"
        )

        has_bounds = bounds and bounds["width"] > 0 and bounds["height"] > 0

        if is_interactive_type and has_bounds and visible and label:
            win_x = win_bounds["x"]
            win_y = win_bounds["y"]
            win_w = win_bounds["width"]
            win_h = win_bounds["height"]

            rel_x = bounds["x"] - win_x
            rel_y = bounds["y"] - win_y

            # Relative click target
            rel_cx = rel_x + bounds["width"] // 2
            rel_cy = rel_y + bounds["height"] // 2

            dist_right = win_w - rel_cx
            dist_bottom = win_h - rel_cy

            ratio_x = rel_cx / win_w if win_w > 0 else 0.0
            ratio_y = rel_cy / win_h if win_h > 0 else 0.0

            region = classify_region(bounds, win_bounds) or "center"
            anchor = determine_anchor(region)

            control_info = {
                "label": label,
                "type": elem_type,
                "role": role,
                "region": region,
                "anchor_style": anchor,
                "absolute_bounds": bounds,
                "relative_bounds": {
                    "x": rel_x,
                    "y": rel_y,
                    "width": bounds["width"],
                    "height": bounds["height"]
                },
                "relative_click_target": {
                    "x": rel_cx,
                    "y": rel_cy
                },
                "anchor_meta": {
                    "dist_from_right": dist_right,
                    "dist_from_bottom": dist_bottom,
                    "ratio_x": ratio_x,
                    "ratio_y": ratio_y
                },
                "action_performed": "Not tested yet"
            }
            if label_inferred:
                control_info["label_inferred"] = True

            self.controls.append(control_info)

        # Recurse to children
        try:
            n_children = acc.get_child_count()
        except Exception:
            n_children = 0

        for i in range(n_children):
            try:
                child = acc.get_child_at_index(i)
            except Exception:
                continue
            self.walk(child, depth + 1, win_bounds)


def get_current_windows(app_name):
    """Returns a set of open window names for the application."""
    titles = set()
    try:
        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app and app_name.lower() in (app.get_name() or "").lower():
                for j in range(app.get_child_count()):
                    w = app.get_child_at_index(j)
                    if w and w.get_role_name() in ('frame', 'dialog'):
                        titles.add(w.get_name())
    except Exception:
        pass
    return titles


def get_current_menus():
    """Returns a list of open menu items on the desktop."""
    menus = []
    try:
        desktop = Atspi.get_desktop(0)
        def walk_menus(acc):
            try:
                role = acc.get_role_name()
                name = acc.get_name()
                if role == 'menu item' and name:
                    menus.append(name)
            except Exception:
                pass
            try:
                for idx in range(acc.get_child_count()):
                    walk_menus(acc.get_child_at_index(idx))
            except Exception:
                pass

        for i in range(desktop.get_child_count()):
            walk_menus(desktop.get_child_at_index(i))
    except Exception:
        pass
    return menus


def confirm_target_window(app_query, title_query=None):
    """Cross-checks the mapping target against the real, current open-window
    inventory (the same merged AT-SPI + X11 list list_windows.py produces)
    *before* map_app.py focuses or clicks anything.

    Why this exists: find_window() in skelo_common does a loose,
    first-match substring search directly against the live AT-SPI tree. On
    its own that's fine for a one-off click, but for mapping it was letting
    map_app.py silently latch onto the wrong window (a same-process helper
    window, an unrelated app that happens to share a substring, or
    whichever accessible object AT-SPI enumerates first) and then save the
    profile under *that* window's name — not the app the caller asked for.
    Requiring an unambiguous match here first means a wrong or missing
    target fails loudly, instead of quietly mapping the wrong application.

    Returns (confirmed_app_name, confirmed_window_title, error_dict_or_None,
    caveat_dict_or_None). `error` means "refuse to map, something is wrong
    or ambiguous." `caveat` means "proceeding, but the identification is
    lower-confidence than usual" — the caller should surface it, not abort.
    """
    windows, warning = scan_open_windows(include_all=False)

    q = app_query.lower()
    matches = [w for w in windows if q in (w.get("app_name") or "").lower()]

    if title_query:
        tq = title_query.lower()
        matches = [w for w in matches if tq in (w.get("window_title") or "").lower()]

    if not matches:
        return None, None, {
            "status": "error",
            "error": f"No open window currently matches app='{app_query}'"
                     + (f", title='{title_query}'" if title_query else "") + ".",
            "hint": "Run list_windows.py to confirm it's actually open (and "
                    "check spelling/casing), or launch it first with "
                    "app_launcher.py --open. map_app.py refuses to map a "
                    "window it can't positively identify.",
        }, None

    distinct_apps = sorted({w["app_name"] for w in matches})
    if len(distinct_apps) > 1:
        return None, None, {
            "status": "error",
            "error": f"'{app_query}' matches {len(distinct_apps)} different open "
                     f"applications — refusing to guess which one to map.",
            "candidates": distinct_apps,
            "hint": "Re-run with a more specific --app value.",
        }, None

    distinct_titles = sorted({w["window_title"] for w in matches if w["window_title"]})
    if len(distinct_titles) > 1 and not title_query:
        return None, None, {
            "status": "error",
            "error": f"'{app_query}' has {len(distinct_titles)} open windows — "
                     f"refusing to guess which one to map.",
            "candidates": distinct_titles,
            "hint": "Add --title to pick the exact window to map.",
        }, None

    chosen = matches[0]
    low_confidence = all(w.get("source") == "x11_fallback" for w in matches)
    caveat = {
        "low_confidence": True,
        "reason": "Identified only via process-name/WM_CLASS heuristics (no "
                  "AT-SPI accessibility data available for this app), which is "
                  "less reliable — double check this is really the intended "
                  "app before trusting the mapped profile.",
        "raw_comm_name": chosen.get("raw_comm_name"),
    } if low_confidence else None
    return chosen["app_name"], chosen["window_title"], None, caveat


def map_application(app_query, title_query=None, output=None):
    """Runs the full mapping flow and returns a result dict. Never raises;
    all failures come back as {"status": "error", ...} so callers (the CLI
    below, or an orchestrator like skelo_learn.py) can handle them
    uniformly without needing subprocess/exit-code plumbing."""

    confirmed_app, confirmed_title, err, caveat = confirm_target_window(app_query, title_query)
    if err:
        return err

    # Focus the *confirmed* window, not the raw (possibly loose) query.
    print(f"Focusing application '{confirmed_app}'...", file=sys.stderr, flush=True)
    subprocess.run(['wmctrl', '-a', confirmed_title or confirmed_app])
    time.sleep(1.0)

    # Resolve via AT-SPI using the confirmed identity, so this can't drift
    # from what confirm_target_window() just verified is actually open.
    app_node, win_node = find_window(confirmed_app, confirmed_title)
    if win_node is None:
        return {
            "status": "error",
            "error": f"'{confirmed_app}' / '{confirmed_title}' was open a moment ago "
                     f"but its AT-SPI window could not be resolved now (closed? "
                     f"still loading?).",
            "hint": "Re-run list_windows.py and try again.",
        }

    win_bounds = get_bounds(win_node)
    if not win_bounds:
        return {"status": "error", "error": "Failed to determine window bounds"}

    app_name = app_node.get_name()
    win_title = win_node.get_name()

    # Belt-and-suspenders: even after resolving via find_window(), verify
    # the AT-SPI node we're about to click actually corresponds to what we
    # confirmed was open. If it doesn't, something raced (a different
    # window of the same app grabbed focus, etc.) — abort rather than map
    # and mislabel the wrong thing.
    if confirmed_title and win_title and confirmed_title != win_title \
            and confirmed_title.lower() not in win_title.lower() \
            and win_title.lower() not in confirmed_title.lower():
        return {
            "status": "error",
            "error": "Identity mismatch: confirmed open window was "
                     f"'{confirmed_title}' but AT-SPI resolved to '{win_title}'.",
            "hint": "This app likely switched windows between the check and "
                    "the mapping pass. Re-run map_app.py.",
        }

    # Scrape layout first
    print("Scraping layout elements...", file=sys.stderr, flush=True)
    mapper = AppMapper()
    mapper.walk(win_node, 0, win_bounds)

    print(f"Discovered {len(mapper.controls)} interactive controls. Testing action mapping...", file=sys.stderr, flush=True)
    
    # We will test click each interactive control and monitor window/menu changes
    # To keep discovery safe, we only test buttons, toggle_buttons, and tabs.
    tested_count = 0
    for control in mapper.controls:
        # Avoid clicking text fields, menu items, or list items that require complex input
        if control["type"] not in ("button", "toggle_button", "tab"):
            control["action_performed"] = "Interactive type - click test bypassed"
            continue

        # Avoid clicking potentially dangerous or disruptive system buttons (e.g. exit, close, quit)
        label_lower = control["label"].lower()
        if any(w in label_lower for w in ("close", "exit", "quit", "delete", "remove", "clear")):
            control["action_performed"] = "Bypassed to prevent application termination or data loss"
            continue

        # Get initial state
        initial_windows = get_current_windows(app_name)
        initial_menus = get_current_menus()

        # Resolve click target
        cx = control["absolute_bounds"]["x"] + control["absolute_bounds"]["width"] // 2
        cy = control["absolute_bounds"]["y"] + control["absolute_bounds"]["height"] // 2

        # Sanity check: do not click off-screen coordinates
        screen_w, screen_h = pyautogui.size()
        if not (0 <= cx <= screen_w and 0 <= cy <= screen_h):
            control["action_performed"] = f"Bypassed because coordinate ({cx}, {cy}) is off-screen"
            print(f" -> Result: Bypassed (off-screen target: {cx}, {cy})", file=sys.stderr, flush=True)
            continue

        # Raise the window to the top layer before clicking to avoid hitting overlapping windows
        try_activate_window(win_node)
        time.sleep(0.2)

        print(f"Testing button '{control['label']}' at ({cx}, {cy})...", file=sys.stderr, flush=True)
        # Move mouse and click
        pyautogui.click(cx, cy)
        time.sleep(0.8) # Wait for UI to update

        # Inspect resulting state
        post_windows = get_current_windows(app_name)
        post_menus = get_current_menus()

        new_windows = post_windows - initial_windows
        new_menus = [m for m in post_menus if m not in initial_menus]

        # Analyze changes
        if new_windows:
            win_list = ", ".join([f"'{w}'" for w in new_windows])
            control["action_performed"] = f"Opens window(s): {win_list}"
            print(f" -> Result: {control['action_performed']}", file=sys.stderr, flush=True)
            # Dismiss new window by pressing Escape
            pyautogui.press('escape')
            time.sleep(0.5)
        elif new_menus:
            menu_list = ", ".join([f"'{m}'" for m in new_menus[:5]])
            if len(new_menus) > 5:
                menu_list += f" and {len(new_menus)-5} more"
            control["action_performed"] = f"Spawns popup menu containing: [{menu_list}]"
            print(f" -> Result: {control['action_performed']}", file=sys.stderr, flush=True)
            # Dismiss menu by pressing Escape
            pyautogui.press('escape')
            time.sleep(0.5)
        else:
            control["action_performed"] = "Triggers internal state change or no visible action"

        tested_count += 1
        # Prevent runaway execution on applications with hundreds of buttons
        if tested_count >= 20:
            print("Mapping test cap reached (20 buttons tested).", file=sys.stderr, flush=True)
            break

    # Save mapping files
    slug = app_name.lower().replace(" ", "_")
    output_path = output if output else os.path.join(_SKILLS_DIR, f"{slug}.json")
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Save JSON Profile
    profile = {
        "app_name": app_name,
        "window_title": win_title,
        "window_bounds": win_bounds,
        "learned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "control_count": len(mapper.controls),
        "controls": mapper.controls
    }

    with open(output_path, "w") as f:
        json.dump(profile, f, indent=2)

    # Save Markdown Profile
    md_path = os.path.splitext(output_path)[0] + ".md"
    md_lines = [
        f"# Skelo App Skill Profile: {app_name.capitalize()}",
        "",
        f"This profile maps the layout, buttons, and controls of **{app_name}**.",
        "It includes relative coordinates and dynamic action-mappings.",
        "",
        "## Window Metadata",
        "",
        f"* **App Name:** `{app_name}`",
        f"* **Window Title:** `{win_title}`",
        f"* **Window Bounds:** `X={win_bounds['x']}, Y={win_bounds['y']}, W={win_bounds['width']}, H={win_bounds['height']}`",
        f"* **Learned At:** `{profile['learned_at']}`",
        f"* **Total Controls:** `{len(mapper.controls)}`",
        "",
        "## Discovered Action Map",
        "",
        "| Control Label | Type | Screen Region | Anchor Style | Relative Target | Action Performed |",
        "|---|---|---|---|---|---|",
    ]

    sorted_controls = sorted(mapper.controls, key=lambda c: (c["type"], c["label"]))
    for c in sorted_controls:
        target = c["relative_click_target"]
        action = c.get("action_performed", "Not tested")
        md_lines.append(f"| **{c['label']}** | `{c['type']}` | *{c['region']}* | `{c['anchor_style']}` | `+{target['x']}, +{target['y']}` | {action} |")

    with open(md_path, "w") as f:
        f.write("\n".join(md_lines) + "\n")

    result = {
        "status": "success",
        "app_name": app_name,
        "window_title": win_title,
        "controls_learned": len(mapper.controls),
        "json_profile": output_path,
        "markdown_profile": md_path,
    }
    if caveat:
        result["identification_caveat"] = caveat
    return result


def main():
    parser = argparse.ArgumentParser(description="Map an application window and test click actions.")
    parser.add_argument("--app", required=True, help="App name substring (case-insensitive)")
    parser.add_argument("--title", default=None, help="Window title substring (case-insensitive)")
    parser.add_argument("--output", default=None, help="Custom output JSON path (defaults to skills/<app_name>.json)")
    args = parser.parse_args()

    result = map_application(args.app, args.title, args.output)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") == "success" else 1)


if __name__ == "__main__":
    main()