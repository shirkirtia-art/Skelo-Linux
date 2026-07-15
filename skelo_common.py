"""
Skelo — skelo_common.py

Single shared module for everything the other Skelo scripts need in common:
AT-SPI tree helpers (find a window, read bounds/state, resolve a label),
role/type mapping, and the human-like cursor-motion math.

Why this file exists: list_windows.py, inspect_window.py, skelo_action.py,
and skelo_resolve.py used to each define their own copies
of get_bounds(), find_window(), ease_in_out_cubic(), etc. That meant a bug
fixed in one copy stayed broken in the other four. Everything below is now
defined exactly once; the other scripts import from here.

This module intentionally has NO __main__ / CLI of its own — it's a library,
not a step in the agent workflow.
"""

import math
import random
import shutil
import subprocess
import sys
import time
import warnings

# Clean environment variables: remove SUDO_* variables to prevent permission errors
import os
for _k in list(os.environ.keys()):
    if _k.startswith("SUDO_"):
        os.environ.pop(_k)


def _ensure_desktop_user():
    """AT-SPI's session bus authenticates callers by peer UID (SO_PEERCRED),
    not by which socket path AT_SPI_BUS_ADDRESS happens to point at. Running
    any Skelo script as root gets that connection silently rejected by the
    AT-SPI registry — Atspi.get_desktop(0) just comes back empty instead of
    erroring — and every script quietly falls back to the much less
    reliable X11/`/proc/<pid>/comm`-based identification path instead.

    This is not just cosmetic: it produced a real bug where the exact same
    PID was reported as "Spotify" under root and as "Chromium" (showing an
    unrelated video title) under `sudo -u <user>`, because Spotify's CEF
    wrapper process gets misread by the crude X11 comm-name heuristic.
    Rather than documenting "always remember to run this as the desktop
    user" and hoping every caller does, we just do it: if we're root, we
    re-exec the whole process as the logged-in desktop user before doing
    anything else. This makes identification consistent no matter how the
    script was invoked.
    """
    if os.name != "posix" or os.environ.get("SKELO_REEXEC_DONE") == "1":
        return
    try:
        if os.getuid() != 0:
            return
    except Exception:
        return

    target_user = os.environ.get("SKELO_DESKTOP_USER")
    if not target_user:
        try:
            import pwd
            candidates = [p for p in pwd.getpwall() if 1000 <= p.pw_uid < 65534]
            # Prefer a candidate with a live graphical session (has a
            # runtime dir), so we don't attach to some other unrelated
            # service account that merely has a uid >= 1000.
            for p in candidates:
                if os.path.isdir(f"/run/user/{p.pw_uid}"):
                    target_user = p.pw_name
                    break
            if not target_user and candidates:
                target_user = candidates[0].pw_name
        except Exception:
            target_user = None

    if not target_user:
        # Can't determine who the desktop user is — fall through to the
        # legacy (less reliable) bus-path guess below rather than crash.
        return

    try:
        env = os.environ.copy()
        env["SKELO_REEXEC_DONE"] = "1"
        os.execvpe("sudo", ["sudo", "-u", target_user, "-E", "--", sys.executable] + sys.argv, env)
    except Exception:
        pass  # if re-exec isn't possible here, continue as root


_ensure_desktop_user()

# Dynamically resolve running user's AT-SPI bus socket to prevent GLib/Atspi
# from reading X11 root window's AT_SPI_BUS property (which points to root's bus /root/.cache/at-spi/bus_0)
# This remains as a last-resort fallback for the rare case _ensure_desktop_user()
# above couldn't re-exec (e.g. sudo unavailable) — it does NOT fix the
# peer-UID rejection, it only helps the (already degraded) legacy path.
try:
    import pwd
    uid = os.getuid()
    if uid == 0:
        uid = 1000
    bus_path = f"/run/user/{uid}/at-spi/bus_0"
    if os.path.exists(bus_path):
        os.environ["AT_SPI_BUS_ADDRESS"] = f"unix:path={bus_path}"
    else:
        home_dir = pwd.getpwuid(uid).pw_dir
        cache_bus = f"{home_dir}/.cache/at-spi/bus_0"
        if os.path.exists(cache_bus):
            os.environ["AT_SPI_BUS_ADDRESS"] = f"unix:path={cache_bus}"
except Exception:
    if os.path.exists("/run/user/1000/at-spi/bus_0"):
        os.environ["AT_SPI_BUS_ADDRESS"] = "unix:path=/run/user/1000/at-spi/bus_0"

try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    ATSPI_AVAILABLE = True
except (ImportError, ValueError) as e:
    ATSPI_AVAILABLE = False
    ATSPI_IMPORT_ERROR = str(e)


def atspi_unavailable_error():
    return {
        "error": "AT-SPI GObject bindings not available",
        "detail": ATSPI_IMPORT_ERROR,
        "fix": (
            "sudo apt install python3-gi gir1.2-atspi-2.0 at-spi2-core && "
            "gsettings set org.gnome.desktop.interface toolkit-accessibility true "
            "(then log out/in so already-running apps pick it up)"
        ),
    }


# --------------------------------------------------------------------------
# Role / type mapping
# --------------------------------------------------------------------------

TOP_LEVEL_ROLES = {"frame", "window", "dialog", "alert"}

ROLE_TYPE_MAP = {
    "push button": "button", "toggle button": "toggle_button", "text": "text_field",
    "entry": "text_field", "password text": "text_field", "label": "label",
    "static": "label", "menu item": "menu_item", "menu": "menu", "check box": "checkbox",
    "check menu item": "checkbox", "radio button": "radio_button",
    "radio menu item": "radio_button", "combo box": "combo_box", "list item": "list_item",
    "list": "list", "list box": "list", "table": "table", "table cell": "table_cell",
    "table row": "table_row", "slider": "slider", "image": "image", "icon": "icon",
    "link": "link", "page tab": "tab", "page tab list": "tab_list", "panel": "panel",
    "scroll bar": "scroll_bar", "scroll pane": "scroll_area", "frame": "window",
    "dialog": "dialog", "tool bar": "toolbar", "separator": "separator",
    "spin button": "spinner", "progress bar": "progress_bar", "section": "section",
}

CLICKABLE_ROLES = {
    "push button", "toggle button", "menu item", "check menu item", "list item",
    "page tab", "link", "check box", "radio button", "radio menu item", "icon",
}

STRUCTURAL_ROLES = {
    "panel", "section", "filler", "scroll pane", "viewport",
    "split pane", "layered pane", "unknown", "redundant object",
    "table cell",
}


def get_value(acc, max_len=300):
    """Reads an element's current value (spinner/slider current_value, or
    text-field contents up to max_len chars). Shared so inspect_window.py's
    and skelo_action.py's 'meaningful' determination — which factors in
    whether a node has a value — agree with each other."""
    try:
        val_iface = acc.get_value_iface()
        if val_iface:
            return val_iface.get_current_value()
    except Exception:
        pass
    try:
        text_iface = acc.get_text_iface()
        if text_iface:
            count = text_iface.get_character_count()
            if count:
                text = text_iface.get_text(0, min(count, max_len))
                if count > max_len:
                    text += "…"
                return text
    except Exception:
        pass
    return None


def is_clickable(acc, role):
    """Shared clickability check — role must be in CLICKABLE_ROLES *and* the
    element must currently be enabled/sensitive. Returns None for roles
    that are never clickable (so callers can distinguish 'not applicable'
    from 'not currently clickable')."""
    if role not in CLICKABLE_ROLES:
        return None
    try:
        states = acc.get_state_set()
        if states is None:
            return False
        return states.contains(Atspi.StateType.ENABLED) and states.contains(Atspi.StateType.SENSITIVE)
    except Exception:
        return False


def is_meaningful_element(elem_type, role, label, value, bounds, clickable, focused, selected, interactive_only=False):
    """Shared 'is this AT-SPI node worth surfacing' predicate.

    Used by both inspect_window.py (to decide what to include in its flat
    output, and what sequential el_N id to assign) and skelo_action.py (to
    decide what counts toward its own el_N numbering during live element
    resolution). Before this was shared, the two scripts used two
    different definitions of "meaningful" — inspect_window.py counted only
    meaningful nodes, skelo_action.py counted every node in the tree — so
    an el_100 from one script's output did not refer to the same element
    in the other. With both walking the same predicate in the same
    traversal order, their numbering lines up."""
    has_area = bool(bounds) and bounds.get("width", 0) > 0 and bounds.get("height", 0) > 0
    is_structural = role in STRUCTURAL_ROLES
    has_content = bool(label) or (value not in (None, ""))
    is_interactive = bool(clickable) or focused or selected

    meaningful = has_area and (has_content or is_interactive or not is_structural) \
        and (has_content or is_interactive or elem_type in ("window", "dialog"))

    if meaningful and interactive_only:
        meaningful = is_interactive or elem_type == "text_field"

    return meaningful


# --------------------------------------------------------------------------
# Live AT-SPI tree helpers
# --------------------------------------------------------------------------

def get_bounds(acc):
    try:
        component = acc.get_component_iface()
        if component:
            extents = component.get_extents(Atspi.CoordType.SCREEN)
            return {"x": extents.x, "y": extents.y, "width": extents.width, "height": extents.height}
    except Exception:
        pass
    return None


def is_state(acc, state):
    try:
        states = acc.get_state_set()
        return bool(states and states.contains(state))
    except Exception:
        return False


_EXTRA_STATE_NAMES = ["CHECKED", "PRESSED", "ARMED", "EXPANDED"]
EXTRA_STATES = {n.lower(): getattr(Atspi.StateType, n) for n in _EXTRA_STATE_NAMES
                if ATSPI_AVAILABLE and getattr(Atspi.StateType, n, None) is not None} if ATSPI_AVAILABLE else {}


def get_state_flags(acc):
    """Full readiness/interaction state dict — used by skelo_action.py to
    decide if an element is ready to be acted on, and by --confirm to
    report what changed."""
    flags = {}
    try:
        states = acc.get_state_set()
    except Exception:
        return flags
    if states is None:
        return flags
    checks = [("showing", Atspi.StateType.SHOWING), ("visible", Atspi.StateType.VISIBLE),
              ("enabled", Atspi.StateType.ENABLED), ("sensitive", Atspi.StateType.SENSITIVE),
              ("focused", Atspi.StateType.FOCUSED), ("selected", Atspi.StateType.SELECTED)]
    checks += list(EXTRA_STATES.items())
    for name, st in checks:
        try:
            flags[name] = bool(states.contains(st))
        except Exception:
            pass
    return flags


def get_description(acc):
    try:
        d = acc.get_description()
        return d or None
    except Exception:
        return None


def infer_label_from_children(acc):
    """Icon-only buttons (a bare [X] close icon, a search glyph, ...) often
    have no name of their own but a labeled image/icon/label child one
    level down. Only looks one level deep to avoid grabbing an unrelated
    descendant's label."""
    try:
        n = acc.get_child_count()
    except Exception:
        n = 0
    for i in range(n):
        try:
            child = acc.get_child_at_index(i)
        except Exception:
            continue
        if child is None:
            continue
        try:
            child_name = child.get_name()
        except Exception:
            child_name = None
        if not child_name:
            continue
        try:
            child_role = child.get_role_name()
        except Exception:
            child_role = None
        if child_role in ("image", "icon", "label", "static"):
            return child_name
    return None


def resolve_label(acc, role):
    """Returns (label, inferred). inferred=True means the label was
    borrowed from description or a child, not the element's own name."""
    label = acc.get_name() or None
    if label:
        return label, False
    if role not in CLICKABLE_ROLES:
        return None, False
    label = get_description(acc)
    if label:
        return label, True
    label = infer_label_from_children(acc)
    if label:
        return label, True
    return None, False


def classify_region(bounds, window_bounds):
    """Coarse 3x3 grid position within the window: 'top-left', 'top-center',
    'top-right', 'middle-left', 'center', 'middle-right', 'bottom-left',
    'bottom-center', 'bottom-right'."""
    if not bounds or not window_bounds:
        return None
    if window_bounds.get("width", 0) <= 0 or window_bounds.get("height", 0) <= 0:
        return None

    cx = bounds["x"] + bounds["width"] / 2
    cy = bounds["y"] + bounds["height"] / 2
    rel_x = (cx - window_bounds["x"]) / window_bounds["width"]
    rel_y = (cy - window_bounds["y"]) / window_bounds["height"]

    col = "left" if rel_x < 1 / 3 else ("right" if rel_x > 2 / 3 else "center")
    row = "top" if rel_y < 1 / 3 else ("bottom" if rel_y > 2 / 3 else "middle")

    if row == "middle" and col == "center":
        return "center"
    return f"{row}-{col}"


def find_window(app_query, title_query):
    """Returns (app_acc, window_acc) for the first app/window matching the
    given case-insensitive substrings, or (None, None)."""
    desktop = Atspi.get_desktop(0)
    app_count = desktop.get_child_count() if desktop else 0
    for i in range(app_count):
        try:
            app = desktop.get_child_at_index(i)
        except Exception:
            continue
        if app is None:
            continue
        app_name = app.get_name() or ""
        if app_query and app_query.lower() not in app_name.lower():
            continue
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
            win_title = win.get_name() or ""
            if title_query and title_query.lower() not in win_title.lower():
                continue
            return app, win
    return None, None


def scroll_into_view(acc):
    try:
        comp = acc.get_component_iface()
        if comp and hasattr(comp, "scroll_to"):
            return bool(comp.scroll_to(Atspi.ScrollType.ANYWHERE))
    except Exception:
        pass
    return False


def try_activate_window(win_acc):
    """Best-effort focus/raise. AT-SPI grab_focus() often but not always
    raises the window too; falls back to wmctrl if installed."""
    activated = False
    try:
        comp = win_acc.get_component_iface()
        if comp:
            activated = bool(comp.grab_focus())
    except Exception:
        pass
    try:
        title = win_acc.get_name() or ""
        if title:
            subprocess.run(["wmctrl", "-a", title], timeout=1,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return activated


def is_window_active(win_acc):
    """True if the window manager currently considers this the
    active/focused window. This is the best signal AT-SPI gives us for
    'nothing else is drawn on top of it' — there's no direct way to read
    raw X11 stacking order from the accessibility tree, but a window that
    isn't ACTIVE/FOCUSED after an activation attempt is a strong sign
    something else (another app's window) is still in front of it."""
    return is_state(win_acc, Atspi.StateType.ACTIVE) or is_state(win_acc, Atspi.StateType.FOCUSED)


def ensure_window_topmost(win_acc, retries=3, retry_delay=0.25):
    """Raises/activates a window and *verifies* it actually became active
    before returning, instead of firing wmctrl -a once and hoping.

    This is the fix for the classic "asked to pause Spotify, it clicked
    Chrome instead" bug: the old try_activate_window() was fire-and-forget,
    so a cursor click could go ahead even when the raise silently failed
    (title mismatch, WM was slow, focus-follows-mouse fighting it, etc.)
    and land on whatever window was actually on top on screen.

    Returns True if activation was confirmed, False otherwise — callers
    should treat False as "a coordinate click here is occlusion-risky" and
    prefer an AT-SPI direct action click instead (see direct_action_click),
    which doesn't depend on what's drawn on top.
    """
    for _attempt in range(retries):
        try_activate_window(win_acc)
        time.sleep(retry_delay)
        if is_window_active(win_acc):
            return True
    return False


def _wmctrl_list_pl():
    """Parses `wmctrl -l -p` into dicts with id/desktop/pid/host/title."""
    try:
        r = subprocess.run(["wmctrl", "-l", "-p"], capture_output=True, text=True, timeout=2)
        if r.returncode != 0:
            return []
        rows = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            win_id, desktop, pid = parts[0], parts[1], parts[2]
            title = parts[4] if len(parts) > 4 else ""
            rows.append({"id": win_id, "desktop": desktop, "pid": pid, "title": title})
        return rows
    except Exception:
        return []


def _xprop(win_id, prop):
    try:
        r = subprocess.run(["xprop", "-id", win_id, prop], capture_output=True, text=True, timeout=2)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _norm_win_id(win_id):
    """Normalizes '0x02c00003' vs '0x2c00003' vs decimal so ids compare
    equal regardless of which tool printed them and how it padded them."""
    try:
        s = win_id.strip()
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except Exception:
        return None


def _active_window_id():
    try:
        out = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], capture_output=True,
                              text=True, timeout=2).stdout
    except Exception:
        return None
    if "#" not in out:
        return None
    raw = out.split("#")[-1].strip().split(",")[0].strip()
    return _norm_win_id(raw)


def resolve_window_id(app_query, title_query=None):
    """Finds the concrete X11 window id for app_query/title_query, the same
    way map_app.py's confirm_target_window() finds a concrete AT-SPI window:
    by cross-checking against the real, current window inventory instead of
    trusting a loose title-string match.

    This exists because wmctrl's own '-r <string>' matching — what
    window_manage() used to hand a raw app/title string straight to — can
    silently match zero windows (e.g. app_name 'Spotify' doesn't appear
    anywhere in Spotify's window title, which is the current song) and
    wmctrl still exits 0 either way. That's what made minimize/maximize/
    raise/close report success while doing nothing on screen. Preferring a
    pid match (via AT-SPI, when available) instead of a title-string guess
    is what fixes that.

    Returns (win_id, resolved_title, pid) or (None, None, None).
    """
    rows = _wmctrl_list_pl()
    if not rows:
        return None, None, None

    target_pid = None
    if ATSPI_AVAILABLE:
        try:
            app_acc, win_acc = find_window(app_query, title_query)
            if win_acc is not None:
                target_pid = app_acc.get_process_id()
        except Exception:
            target_pid = None

    if target_pid:
        pid_matches = [r for r in rows if r["pid"] == str(target_pid)]
        if title_query:
            tq = title_query.lower()
            narrowed = [r for r in pid_matches if tq in r["title"].lower()]
            if narrowed:
                pid_matches = narrowed
        if pid_matches:
            return pid_matches[0]["id"], pid_matches[0]["title"], target_pid

    # AT-SPI couldn't resolve a pid (app not accessible, or not found) —
    # fall back to matching wmctrl's own title/WM_CLASS, same heuristic
    # list_windows.py's X11 fallback path already uses elsewhere.
    q = (title_query or app_query or "").lower()
    if q:
        for r in rows:
            if q in r["title"].lower():
                return r["id"], r["title"], r["pid"]
    if app_query:
        aq = app_query.lower()
        for r in rows:
            wm_class = _xprop(r["id"], "WM_CLASS").split("=", 1)[-1].lower()
            if aq in wm_class:
                return r["id"], r["title"], r["pid"]

    return None, None, None


def _verify_window_op(win_id, op):
    if op == "raise":
        active, target = _active_window_id(), _norm_win_id(win_id)
        return active is not None and target is not None and active == target
    state = _xprop(win_id, "_NET_WM_STATE")
    if op == "minimize":
        return "_NET_WM_STATE_HIDDEN" in state
    if op == "maximize":
        return "_NET_WM_STATE_MAXIMIZED_VERT" in state and "_NET_WM_STATE_MAXIMIZED_HORZ" in state
    if op == "unmaximize":
        return "_NET_WM_STATE_MAXIMIZED_VERT" not in state and "_NET_WM_STATE_MAXIMIZED_HORZ" not in state
    if op == "close":
        return not any(r["id"] == win_id for r in _wmctrl_list_pl())
    return True


def window_manage(app_query, op, title_query=None, retries=3, retry_delay=0.3):
    """Direct window-level operations, resolved to a concrete X11 window id
    (not a loose title string) and *verified* against the window's actual
    _NET_WM_STATE / _NET_ACTIVE_WINDOW afterward — see resolve_window_id()
    and _verify_window_op() for why the old string-matching version could
    report success without doing anything.

    op: 'raise' | 'minimize' | 'maximize' | 'unmaximize' | 'close'
    Returns (ok, error_or_None). ok is only True if the state change was
    actually observed, not merely that a command was sent.
    """
    win_id, resolved_title, _pid = resolve_window_id(app_query, title_query)
    if not win_id:
        return False, (
            f"No open window found matching app='{app_query}'"
            + (f", title='{title_query}'" if title_query else "") + ". "
            "Run `skelo.sh windows` to see exact names — window titles "
            "(e.g. Spotify's is the current song) often don't contain the "
            "app name, so add --title if a plain app match isn't finding it."
        )

    have_xdotool = shutil.which("xdotool") is not None
    try:
        for _attempt in range(retries):
            if op == "raise":
                subprocess.run(["wmctrl", "-i", "-a", win_id], timeout=2,
                                capture_output=True, text=True)
            elif op == "minimize":
                if have_xdotool:
                    subprocess.run(["xdotool", "windowminimize", win_id], timeout=2,
                                    capture_output=True, text=True)
                else:
                    subprocess.run(["wmctrl", "-i", "-r", win_id, "-b", "add,hidden"],
                                    timeout=2, capture_output=True, text=True)
            elif op == "maximize":
                subprocess.run(["wmctrl", "-i", "-r", win_id, "-b",
                                 "add,maximized_vert,maximized_horz"], timeout=2,
                                capture_output=True, text=True)
            elif op == "unmaximize":
                subprocess.run(["wmctrl", "-i", "-r", win_id, "-b",
                                 "remove,maximized_vert,maximized_horz"], timeout=2,
                                capture_output=True, text=True)
            elif op == "close":
                subprocess.run(["wmctrl", "-i", "-c", win_id], timeout=2,
                                capture_output=True, text=True)
            else:
                return False, f"Unknown window op '{op}'"

            time.sleep(retry_delay)
            if _verify_window_op(win_id, op):
                return True, None

        tool_hint = "" if op != "minimize" or have_xdotool else " (xdotool not found — install it for reliable minimize)"
        return False, (
            f"Sent '{op}' to '{resolved_title}' but could not confirm it took effect "
            f"after {retries} attempts{tool_hint}. Run `skelo.sh doctor` to check for "
            f"missing tools, or the window manager may not support this operation."
        )
    except FileNotFoundError as e:
        return False, f"Required tool not installed: {e}"
    except Exception as e:
        return False, str(e)


def direct_action_click(acc):
    """AT-SPI native 'invoke default action' — bypasses screen coordinates
    entirely. Returns (success, error_message_or_None)."""
    try:
        action_iface = acc.get_action_iface()
        if not action_iface:
            return False, "Element has no Action interface"
        n = action_iface.get_n_actions()
        if n == 0:
            return False, "Element exposes zero actions"
        idx = 0
        for i in range(n):
            try:
                name = (action_iface.get_action_name(i) or "").lower()
            except Exception:
                name = ""
            if name in ("click", "press", "activate", "jump"):
                idx = i
                break
        ok = action_iface.do_action(idx)
        return bool(ok), None
    except Exception as e:
        return False, str(e)


def bounds_contained(inner, outer, tol=2):
    if not inner or not outer:
        return False
    return (
        inner["x"] >= outer["x"] - tol
        and inner["y"] >= outer["y"] - tol
        and inner["x"] + inner["width"] <= outer["x"] + outer["width"] + tol
        and inner["y"] + inner["height"] <= outer["y"] + outer["height"] + tol
    )


def dedupe(windows):
    """Collapse exact-duplicate window entries (same app/pid/title/role/
    bounds, differing only in is_focused), preferring the focused copy."""
    deduped = {}
    for w in windows:
        b = w["bounds"]
        bounds_key = (b["x"], b["y"], b["width"], b["height"]) if b else None
        key = (w["app_name"], w["process_id"], w["window_title"], w["role"], bounds_key)
        if key not in deduped or (w["is_focused"] and not deduped[key]["is_focused"]):
            deduped[key] = w
    return list(deduped.values())


def filter_nested_subframes(windows):
    """Drop untitled frames fully contained inside another titled window
    from the same process (e.g. Chrome's internal omnibox/popup panes)."""
    result = []
    for w in windows:
        nested = False
        if not w["window_title"]:
            for other in windows:
                if other is w or other["process_id"] != w["process_id"]:
                    continue
                if other["window_title"] and bounds_contained(w["bounds"], other["bounds"]):
                    nested = True
                    break
        if not nested:
            result.append(w)
    return result


def get_screen_resolution():
    """AT-SPI doesn't expose screen geometry; shell out to xrandr."""
    import re
    try:
        out = subprocess.run(["xrandr", "--current"], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"current\s+(\d+)\s*x\s*(\d+)", out)
        if m:
            return {"width": int(m.group(1)), "height": int(m.group(2))}
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# Human-like cursor motion (shared by skelo_action.py and skelo_action.py)
# --------------------------------------------------------------------------

def ease_in_out_cubic(t):
    if t < 0.5:
        return 4 * t * t * t
    p = -2 * t + 2
    return 1 - (p ** 3) / 2


def cubic_bezier(p0, p1, p2, p3, t):
    x = ((1 - t) ** 3) * p0[0] + 3 * ((1 - t) ** 2) * t * p1[0] \
        + 3 * (1 - t) * (t ** 2) * p2[0] + (t ** 3) * p3[0]
    y = ((1 - t) ** 3) * p0[1] + 3 * ((1 - t) ** 2) * t * p1[1] \
        + 3 * (1 - t) * (t ** 2) * p2[1] + (t ** 3) * p3[1]
    return x, y


def build_curved_path(start, end, curviness=0.35, jitter=1.2):
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    distance = math.hypot(dx, dy)
    if distance < 2:
        return [end]

    perp = (-dy / distance, dx / distance)
    bow = distance * curviness * random.uniform(0.4, 1.0) * random.choice([-1, 1])

    c1 = (sx + dx * 0.33 + perp[0] * bow + random.uniform(-8, 8),
          sy + dy * 0.33 + perp[1] * bow + random.uniform(-8, 8))
    c2 = (sx + dx * 0.66 + perp[0] * bow * 0.6 + random.uniform(-8, 8),
          sy + dy * 0.66 + perp[1] * bow * 0.6 + random.uniform(-8, 8))

    steps = int(min(max(distance / 6, 14), 70))
    path = []
    for i in range(1, steps + 1):
        t = i / steps
        x, y = cubic_bezier(start, c1, c2, end, t)
        if i != steps:
            x += random.uniform(-jitter, jitter)
            y += random.uniform(-jitter, jitter)
        path.append((x, y))
    return path


def move_along_path(pyautogui_mod, path, total_duration=None):
    if total_duration is None:
        total_duration = random.uniform(0.35, 0.9)
    n = len(path)
    for i, (x, y) in enumerate(path):
        eased = ease_in_out_cubic((i + 1) / n)
        prev_eased = ease_in_out_cubic(i / n)
        step_time = max(total_duration * (eased - prev_eased), 0)
        pyautogui_mod.moveTo(x, y)
        if step_time:
            time.sleep(step_time)


def resolve_click_point(bounds, mode="center-jitter"):
    cx, cy = bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2
    if mode == "center":
        return cx, cy
    jx = bounds["width"] * 0.25 * random.uniform(-1, 1)
    jy = bounds["height"] * 0.25 * random.uniform(-1, 1)
    return cx + jx, cy + jy


def load_pyautogui():
    """Import pyautogui with the standard Skelo settings, or return
    (None, error_dict) if unavailable."""
    try:
        import pyautogui as _pag
        _pag.FAILSAFE = False
        _pag.MINIMUM_DURATION = 0
        _pag.MINIMUM_SLEEP = 0
        _pag.PAUSE = 0
        return _pag, None
    except Exception as e:
        return None, {
            "error": "pyautogui not available or no display/X11 session found",
            "detail": str(e),
            "fix": (
                "pip install pyautogui (plus python3-tk python3-dev). Requires an X11 "
                "session — on Wayland, log in via 'Ubuntu on Xorg', or (for skelo_action.py "
                "only) use --method action, which never touches the mouse."
            ),
        }