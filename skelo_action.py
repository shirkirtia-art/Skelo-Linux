#!/usr/bin/env python3
"""
Skelo — skelo_action.py

The unified action executor. Supports both coordinate-based actions and live
AT-SPI element resolution (finding by label/role/element_id) followed by an action.

Coordinate-based actions:
    Move, click, drag, scroll, type, press keys, or draw shapes using screen coordinates.

Element-resolved actions:
    Find elements live via accessibility layer (by label, role, index, etc.)
    and then act on them (move, click, type, etc.).

Supports sequences of mixed actions (some coordinate, some resolved).
"""

import argparse
import json
import math
import os
import random
import sys
import time
import warnings

# Suppress deprecation warnings from PyGObject/AT-SPI
warnings.filterwarnings("ignore", category=DeprecationWarning)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skelo_common import (
    ATSPI_AVAILABLE, atspi_unavailable_error,
    ROLE_TYPE_MAP, CLICKABLE_ROLES, find_window, get_bounds, get_state_flags,
    scroll_into_view, try_activate_window, direct_action_click,
    ease_in_out_cubic, cubic_bezier, build_curved_path, move_along_path,
    resolve_click_point, load_pyautogui, resolve_label,
    is_clickable, is_meaningful_element, get_value, is_state,
    ensure_window_topmost, window_manage,
)

if ATSPI_AVAILABLE:
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError):
        ATSPI_AVAILABLE = False

pyautogui = None


# --------------------------------------------------------------------------
# Coordinate Action Executions
# --------------------------------------------------------------------------

def action_move(x, y, curviness=0.35, duration=None):
    start = pyautogui.position()
    path = build_curved_path((float(start[0]), float(start[1])), (float(x), float(y)), curviness=curviness)
    move_along_path(pyautogui, path, total_duration=duration)
    return {"start": start, "end": [x, y], "waypoints": len(path)}


def action_click(x, y, button="left", clicks=1, curviness=0.35, duration=None):
    result = action_move(x, y, curviness, duration)
    pyautogui.click(x, y, clicks=clicks, button=button)
    result.update({"action": "click", "button": button, "clicks": clicks})
    return result


def action_drag(x, y, x2, y2, curviness=0.35, duration=None):
    action_move(x, y, curviness, duration)
    pyautogui.mouseDown(button="left")
    time.sleep(0.1)

    path = build_curved_path((float(x), float(y)), (float(x2), float(y2)), curviness=curviness)
    move_along_path(pyautogui, path, total_duration=duration)

    time.sleep(0.1)
    pyautogui.mouseUp(button="left")
    return {"action": "drag", "start": [x, y], "end": [x2, y2], "waypoints": len(path)}


def action_scroll(amount):
    pyautogui.scroll(amount)
    return {"action": "scroll", "amount": amount}


def action_type(text):
    pyautogui.write(text, interval=random.uniform(0.02, 0.08))
    return {"action": "type", "text": text}


def action_keypress(key):
    if "+" in key:
        parts = key.split("+")
        pyautogui.hotkey(*parts)
    else:
        pyautogui.press(key)
    return {"action": "keypress", "key": key}


def action_draw_circle(cx, cy, radius, steps=40, duration=1.5):
    start_x = cx + radius
    start_y = cy
    action_move(start_x, start_y, duration=0.5)

    pyautogui.mouseDown(button="left")
    time.sleep(0.1)

    step_duration = duration / steps
    for i in range(1, steps + 1):
        theta = (i / steps) * 2.0 * math.pi
        tx = cx + radius * math.cos(theta) + random.uniform(-0.8, 0.8)
        ty = cy + radius * math.sin(theta) + random.uniform(-0.8, 0.8)
        pyautogui.moveTo(tx, ty)
        time.sleep(step_duration)

    time.sleep(0.1)
    pyautogui.mouseUp(button="left")
    return {"action": "draw_circle", "center": [cx, cy], "radius": radius}


# --------------------------------------------------------------------------
# Element Resolution
# --------------------------------------------------------------------------

def search_element(win_acc, label=None, role=None, element_id=None, max_depth=30, max_elements=5000):
    """Fresh depth-first walk of the live tree. Returns candidate dicts:
    {acc, node, depth}. el_N ids are assigned only to 'meaningful' nodes,
    in the same traversal order and by the same predicate inspect_window.py
    uses, so an id from inspect_window.py's output can be passed straight
    into --element-id here and resolve to the same element."""
    candidates = []
    counter = {"visited": 0, "meaningful": 0}

    def walk(acc, depth=0):
        if acc is None or depth > max_depth or counter["visited"] >= max_elements:
            return
        counter["visited"] += 1
        try:
            raw_role = acc.get_role_name()
        except Exception:
            raw_role = "unknown"
        elem_type = ROLE_TYPE_MAP.get(raw_role, raw_role.replace(" ", "_") if raw_role else "unknown")

        name, label_inferred = resolve_label(acc, raw_role)
        bounds = get_bounds(acc)
        value = get_value(acc)
        clickable_state = is_clickable(acc, raw_role)
        focused = is_state(acc, Atspi.StateType.FOCUSED)
        selected = is_state(acc, Atspi.StateType.SELECTED)

        # Same predicate inspect_window.py uses to decide what to emit —
        # and, critically, the same rule for when to hand out the next
        # el_N. This is what makes an id copied from inspect_window.py's
        # output resolve to the *same* element here, instead of an
        # unrelated node elsewhere in the tree (previously this counted
        # every single node, meaningful or not, so the two scripts' el_N
        # numbering silently diverged).
        meaningful = is_meaningful_element(
            elem_type, raw_role, name, value, bounds, clickable_state, focused, selected,
        )
        node_id = None
        if meaningful:
            counter["meaningful"] += 1
            node_id = f"el_{counter['meaningful']}"

        match = True
        if element_id and node_id != element_id:
            match = False
        if match and label and not (name and label.lower() in name.lower()):
            match = False
        if match and role and role.lower() not in (raw_role.lower(), elem_type.lower()):
            match = False
        if match and element_id and node_id is None:
            match = False

        if match:
            node = {"type": elem_type, "id": node_id, "label": name, "role": raw_role}
            if node_id is None:
                node["id_note"] = ("not numbered by inspect_window.py "
                                    "(structural/non-meaningful element)")
            if label_inferred:
                node["label_inferred"] = True
            if bounds:
                node["bounds"] = bounds
            clickable = raw_role in CLICKABLE_ROLES
            node["clickable"] = clickable
            candidates.append({"acc": acc, "node": node, "depth": depth})

        try:
            n_children = acc.get_child_count()
        except Exception:
            n_children = 0
        for i in range(n_children):
            if counter["visited"] >= max_elements:
                break
            try:
                child = acc.get_child_at_index(i)
            except Exception:
                continue
            walk(child, depth + 1)

    walk(win_acc)
    candidates.sort(key=lambda c: (c["node"]["clickable"] is not True, c["depth"]))
    return candidates


def resolve_element(act_dict):
    """
    Given an action dictionary, if it contains element-query fields,
    resolves the element coordinates and updates the dictionary with 'x' and 'y'.
    Returns (resolved_acc, result_info) or (None, error_dict).

    Important distinction: 'app'/'title' identify a *window*; 'label'/
    'role'/'element_id' identify a *specific element* within it. A bare
    app/title with none of the latter is a request to target the window
    (e.g. so a click/type can be occlusion-aware, or so the window gets
    raised/focused) — it is NOT a request to click "whatever AT-SPI finds
    first" inside that window. Treating it as one used to mean an
    untargeted `type --app gnome-calculator` would search with no label
    filter, match every meaningful element, and click the first one that
    sorted highest (an arbitrary button — "Undo" in one observed case)
    purely to "focus" the window before typing. Now, without an explicit
    label/role/element_id, no element search happens and no click occurs;
    the window is still found and raised, but the caller's own x/y (if
    any) are left untouched, and 'type' with no target just types into
    whatever already has focus after the window is raised.
    """
    app = act_dict.get("app")
    title = act_dict.get("title")
    label = act_dict.get("label")
    role = act_dict.get("role")
    element_id = act_dict.get("element_id")
    index = int(act_dict.get("index", 0))
    retries = int(act_dict.get("retries", 3))
    retry_delay = float(act_dict.get("retry_delay", 0.6))
    max_depth = int(act_dict.get("max_depth", 30))
    max_elements = int(act_dict.get("max_elements", 5000))
    no_activate = bool(act_dict.get("no_activate", False))
    click_point = act_dict.get("click_point", "center-jitter")

    has_element_query = any(k in act_dict for k in ("label", "role", "element_id"))
    has_window_query = any(k in act_dict for k in ("app", "title"))

    # If no query fields are present at all, it's a plain coordinate action.
    if not has_element_query and not has_window_query:
        return None, None

    if not ATSPI_AVAILABLE:
        return None, atspi_unavailable_error()

    attempts_log = []
    chosen = None
    win_acc = app_acc = None
    bounds = None
    state_flags = None

    for attempt in range(retries + 1):
        app_acc, win_acc = find_window(app, title)
        if win_acc is None:
            attempts_log.append({"attempt": attempt, "error": "window not found"})
            time.sleep(retry_delay)
            continue

        window_confirmed_topmost = None
        if not no_activate:
            window_confirmed_topmost = ensure_window_topmost(win_acc)

        if not has_element_query:
            # Window-only resolution: found and raised the window, but the
            # caller didn't ask for a specific element, so don't search for
            # or click one. Return acc=None so callers (e.g. the 'type'
            # branch below) know no element was targeted.
            return None, {
                "app_name": app_acc.get_name() if app_acc else None,
                "window_title": win_acc.get_name() if win_acc else None,
                "window_confirmed_topmost": window_confirmed_topmost,
                "element_targeted": False,
            }

        candidates = search_element(
            win_acc, label=label, role=role, element_id=element_id,
            max_depth=max_depth, max_elements=max_elements
        )

        if act_dict.get("list_candidates"):
            return None, {
                "status": "success",
                "app_name": app_acc.get_name() if app_acc else None,
                "window_title": win_acc.get_name() if win_acc else None,
                "candidate_count": len(candidates),
                "candidates": [c["node"] for c in candidates],
            }

        if not candidates:
            attempts_log.append({"attempt": attempt, "error": "no matching element found"})
            time.sleep(retry_delay)
            continue

        if index >= len(candidates):
            return None, {
                "error": f"index {index} out of range: only {len(candidates)} matching element(s) found",
                "candidates": [c["node"] for c in candidates],
            }

        pick = candidates[index]
        acc = pick["acc"]

        scrolled = scroll_into_view(acc)
        bounds = get_bounds(acc)
        if not bounds or bounds["width"] <= 0 or bounds["height"] <= 0:
            attempts_log.append({"attempt": attempt, "error": "element has no visible bounds", "scrolled": scrolled})
            time.sleep(retry_delay)
            continue

        state_flags = get_state_flags(acc)
        # Robust check: allow showing to be False if visible is True
        is_showing = state_flags.get("showing", True)
        is_visible = state_flags.get("visible", True)
        is_enabled = state_flags.get("enabled", True)
        
        if (not is_showing and not is_visible) or not is_enabled:
            attempts_log.append({"attempt": attempt, "error": "element not ready", "state": state_flags})
            time.sleep(retry_delay)
            continue

        chosen = {"acc": acc, "node": pick["node"], "candidate_count": len(candidates), "scrolled": scrolled}
        break

    if chosen is None:
        return None, {
            "error": "Could not resolve a clickable element after retries",
            "app_query": app, "title_query": title,
            "label_query": label, "role_query": role,
            "attempts": attempts_log,
        }

    acc = chosen["acc"]
    target = resolve_click_point(bounds, mode=click_point)
    
    # Update act_dict coordinates
    act_dict["x"] = target[0]
    act_dict["y"] = target[1]
    
    info = {
        "app_name": app_acc.get_name() if app_acc else None,
        "window_title": win_acc.get_name() if win_acc else None,
        "resolved_element": chosen["node"],
        "candidate_count": chosen["candidate_count"],
        "index_used": index,
        "scrolled_into_view": chosen["scrolled"],
        "pre_click_state": state_flags,
        "target_point": {"x": target[0], "y": target[1]},
        "window_confirmed_topmost": window_confirmed_topmost,
    }
    return acc, info


# --------------------------------------------------------------------------
# Single Action Runner
# --------------------------------------------------------------------------

def run_window_or_key_action(act, act_dict):
    """Window-level operations and Alt+Tab-style cycling — deliberately
    separate from run_single_action's AT-SPI element/click handling. These
    exist so an agent can clear an overlapping window out of the way
    directly ("minimize Chrome", "alt-tab to Spotify") instead of only
    ever being able to click coordinates and hope nothing is in the way."""
    if act == "alt_tab":
        times = int(act_dict.get("times", 1))
        hold_shift = bool(act_dict.get("reverse", False))
        for _ in range(times):
            if hold_shift:
                pyautogui.hotkey("alt", "shift", "tab")
            else:
                pyautogui.hotkey("alt", "tab")
            time.sleep(0.15)
        return {"action": "alt_tab", "times": times, "reverse": hold_shift}

    app_q = act_dict.get("app")
    title_q = act_dict.get("title")
    if not app_q and not title_q:
        raise ValueError(f"Action '{act}' requires 'app' or 'title'")
    op = {"raise_window": "raise", "close_window": "close"}.get(act, act)
    ok, err = window_manage(app_q, op, title_query=title_q)
    res = {"action": act, "app": app_q, "title": title_q, "ok": ok}
    if err:
        res["error"] = err
    return res


def run_single_action(act_dict):
    act = act_dict.get("action")
    
    if act == "sleep":
        duration = float(act_dict.get("duration", 1.0))
        time.sleep(duration)
        return {"action": "sleep", "duration": duration}

    if act in ("minimize", "maximize", "unmaximize", "raise_window", "close_window", "alt_tab"):
        # These don't touch the AT-SPI tree at all (window_manage uses
        # wmctrl/xdotool directly, alt_tab is a pure keypress), so skip
        # resolve_element — otherwise its 'app'/'title' would be
        # (mis)treated as an element query instead of a window target.
        return run_window_or_key_action(act, act_dict)

    if not act:
        if act_dict.get("click"):
            act = "click"
        elif any(k in act_dict for k in ("app", "title", "label", "role", "element_id")):
            act = "click" if act_dict.get("click") else "move"
        else:
            raise ValueError("Each action object must have an 'action' field or valid query parameters")

    acc, res_info = resolve_element(act_dict)
    if res_info and "error" in res_info:
        return res_info
    elif res_info and res_info.get("status") == "success":
        return res_info

    # Hard occlusion gate. If the caller named a target window (--app/
    # --title) and it could NOT be confirmed as actually on top, sending
    # real OS-level input now is a gamble: it goes wherever focus actually
    # is, which may be a completely different app. click already has a
    # safe fallback when a specific AT-SPI element was resolved (invoke it
    # directly, bypassing screen position entirely) — but type, keypress,
    # scroll, and drag have no such fallback, and previously didn't check
    # this at all. This is the concrete fix for "asked to play Spotify,
    # agent typed/clicked into Chrome instead": rather than best-effort
    # warning after the fact, these now refuse before acting, unless the
    # caller explicitly opts out with 'force': true / --force.
    force = bool(act_dict.get("force", False))
    unconfirmed = bool(res_info) and res_info.get("window_confirmed_topmost") is False
    no_safe_fallback = {"type", "keypress", "scroll", "drag"}
    if unconfirmed and not force and (act in no_safe_fallback or (act == "click" and not acc)):
        target_desc = res_info.get("window_title") or res_info.get("app_name") or "the target window"
        return {
            "status": "error",
            "error": (
                f"Refusing to run '{act}': '{target_desc}' could not be confirmed as the "
                "topmost/focused window, and this action has no coordinate-independent "
                "fallback the way a labeled click does — sending it now risks landing on "
                "whatever window actually has focus instead (this is the 'clicked Chrome "
                "instead of Spotify' failure mode)."
            ),
            "hint": (
                "Raise the target explicitly first (`skelo.sh raise --app \"<name>\"`), then "
                "retry — or, for click specifically, target a specific control with --label "
                "so it can use a safe AT-SPI action instead of a screen coordinate. If you're "
                "certain this is safe, pass 'force': true (or --force on the CLI)."
            ),
            "window_confirmed_topmost": False,
        }

    if act == "move":
        if "x" not in act_dict or "y" not in act_dict:
            raise ValueError("Action 'move' requires 'x' and 'y' coordinates or valid element query")
        res = action_move(
            float(act_dict["x"]), float(act_dict["y"]),
            curviness=float(act_dict.get("curviness", 0.35)),
            duration=act_dict.get("duration")
        )
        if res_info:
            res.update(res_info)
        return res

    elif act == "click":
        if "x" not in act_dict or "y" not in act_dict:
            raise ValueError("Action 'click' requires 'x' and 'y' coordinates or valid element query")
            
        method = act_dict.get("method", "cursor")
        confirm = bool(act_dict.get("confirm", False))

        # Occlusion safety: if the target window could not be confirmed as
        # the active/topmost window (see ensure_window_topmost), a plain
        # coordinate click is risky — it lands on whatever screen pixel is
        # actually on top, which may be a different app entirely (this is
        # the "asked for Spotify, clicked Chrome" bug). When that's the
        # case and the caller didn't explicitly force method="cursor",
        # prefer the AT-SPI direct action click instead: it activates the
        # control through the accessibility API, not the screen, so it
        # can't miss regardless of what's drawn on top.
        occlusion_risk = bool(res_info) and res_info.get("window_confirmed_topmost") is False
        auto_switched_for_occlusion = False
        if occlusion_risk and method == "cursor" and act_dict.get("method") != "cursor" and acc:
            method = "action"
            auto_switched_for_occlusion = True

        if method == "action" and acc:
            ok, err = direct_action_click(acc)
            res = {"clicked": ok, "method_used": "action"}
            if err:
                res["action_error"] = err
                if auto_switched_for_occlusion:
                    # No AT-SPI action available either — fall back to a
                    # cursor click, but say so plainly rather than clicking
                    # silently into a possible occlusion.
                    res = action_click(
                        float(act_dict["x"]), float(act_dict["y"]),
                        button=act_dict.get("button", "left"),
                        clicks=int(act_dict.get("clicks", 1)),
                        curviness=float(act_dict.get("curviness", 0.35)),
                        duration=act_dict.get("duration")
                    )
                    res["method_used"] = "cursor"
                    res["occlusion_warning"] = (
                        "Target window could not be confirmed topmost and had no "
                        "AT-SPI action to fall back on; this click used screen "
                        "coordinates and may have hit an overlapping window."
                    )
            if auto_switched_for_occlusion:
                res["auto_switched_to_action"] = True
        else:
            res = action_click(
                float(act_dict["x"]), float(act_dict["y"]),
                button=act_dict.get("button", "left"),
                clicks=int(act_dict.get("clicks", 1)),
                curviness=float(act_dict.get("curviness", 0.35)),
                duration=act_dict.get("duration")
            )
            res["method_used"] = "cursor"
            if occlusion_risk:
                res["occlusion_warning"] = (
                    "Target window could not be confirmed topmost before this "
                    "click; it may have landed on an overlapping window instead."
                )

            if method == "auto" and acc:
                time.sleep(0.15)
                pre_state = res_info.get("pre_click_state", {}) if res_info else {}
                after_flags = get_state_flags(acc)
                if after_flags == pre_state:
                    ok, err = direct_action_click(acc)
                    res["auto_fallback_triggered"] = True
                    res["auto_fallback_succeeded"] = ok
                    if err:
                        res["auto_fallback_error"] = err
                    if ok:
                        res["method_used"] = "cursor+action_fallback"
                        
        if res_info:
            res.update(res_info)
            
        if confirm and res_info:
            time.sleep(0.3)
            app = act_dict.get("app")
            title = act_dict.get("title")
            label = act_dict.get("label")
            role = act_dict.get("role")
            max_depth = int(act_dict.get("max_depth", 30))
            max_elements = int(act_dict.get("max_elements", 5000))
            
            app2, win2 = find_window(app, title)
            after = {"window_still_open": win2 is not None}
            if win2 is not None:
                again = search_element(
                    win2, label=label, role=role,
                    max_depth=max_depth, max_elements=max_elements
                )
                after["matching_elements_now"] = len(again)
                if again:
                    after["first_match_state"] = get_state_flags(again[0]["acc"])
            res["after"] = after
            
        return res

    elif act == "drag":
        if "x" not in act_dict or "y" not in act_dict or "x2" not in act_dict or "y2" not in act_dict:
            raise ValueError("Action 'drag' requires 'x', 'y', 'x2', and 'y2' coordinates")
        res = action_drag(
            float(act_dict["x"]), float(act_dict["y"]),
            float(act_dict["x2"]), float(act_dict["y2"]),
            curviness=float(act_dict.get("curviness", 0.35)),
            duration=act_dict.get("duration")
        )
        if res_info:
            res.update(res_info)
        return res

    elif act == "scroll":
        if "amount" not in act_dict:
            raise ValueError("Action 'scroll' requires 'amount'")
        return action_scroll(int(act_dict["amount"]))

    elif act == "type":
        if acc:
            # Only click when an element was actually resolved (i.e. the
            # caller gave --label/--role/--element-id). A bare --app with
            # no target no longer reaches this point with a fake resolved
            # element — resolve_element() now returns acc=None for that
            # case, so an untargeted type just relies on the window having
            # been raised/focused (see resolve_element's window-only path)
            # instead of clicking whatever AT-SPI happened to find first.
            action_click(
                float(act_dict["x"]), float(act_dict["y"]),
                button="left", clicks=1,
                curviness=float(act_dict.get("curviness", 0.35)),
                duration=act_dict.get("duration")
            )
            time.sleep(0.1)
        if "text" not in act_dict:
            raise ValueError("Action 'type' requires 'text'")
        res = action_type(str(act_dict["text"]))
        if res_info:
            res.update(res_info)
        return res

    elif act == "keypress":
        if "key" not in act_dict:
            raise ValueError("Action 'keypress' requires 'key'")
        return action_keypress(str(act_dict["key"]))

    elif act == "draw_circle":
        if "x" not in act_dict or "y" not in act_dict or "radius" not in act_dict:
            raise ValueError("Action 'draw_circle' requires 'x', 'y', and 'radius'")
        return action_draw_circle(
            float(act_dict["x"]), float(act_dict["y"]),
            float(act_dict["radius"]),
            steps=int(act_dict.get("steps", 40)),
            duration=float(act_dict.get("duration", 1.5))
        )
    else:
        raise ValueError(f"Unknown action type: {act}")


# --------------------------------------------------------------------------
# Sequence Runner
# --------------------------------------------------------------------------

def run_sequence(steps):
    """Runs each step dict in order. Supports mixed coordinate and resolved actions."""
    results = []
    for idx, step in enumerate(steps):
        sleep_after = step.pop("sleep_after", 0.0) if "sleep_after" in step else 0.0
        continue_on_error = step.pop("continue_on_error", False) if "continue_on_error" in step else False

        if not step.get("action") and not any(k in step for k in ("app", "title", "label", "role", "element_id")):
            if sleep_after > 0:
                time.sleep(sleep_after)
                results.append({"sequence_index": idx, "action": "sleep", "duration": sleep_after})
            continue

        try:
            step_result = run_single_action(step)
        except Exception as e:
            step_result = {"error": str(e)}

        step_result["sequence_index"] = idx
        results.append(step_result)

        if "error" in step_result and not continue_on_error:
            return {"status": "error", "stopped_at_step": idx, "results": results}

        if sleep_after > 0:
            time.sleep(sleep_after)

    return {"status": "success", "actions_executed": len(results), "results": results}


# --------------------------------------------------------------------------
# Main Entry Point
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Unified Skelo Action Executor: coordinate actions & live element resolution."
    )
    # Action type
    parser.add_argument("--action", choices=[
        "move", "click", "drag", "scroll", "type", "keypress", "draw_circle",
        "minimize", "maximize", "unmaximize", "raise_window", "close_window", "alt_tab",
    ])
    
    # Coordinate parameters
    parser.add_argument("--x", type=float, help="Target X coordinate.")
    parser.add_argument("--y", type=float, help="Target Y coordinate.")
    parser.add_argument("--x2", type=float, help="Destination X coordinate (for drag-and-drop).")
    parser.add_argument("--y2", type=float, help="Destination Y coordinate (for drag-and-drop).")
    parser.add_argument("--button", default="left", choices=["left", "right", "middle"], help="Mouse button.")
    parser.add_argument("--clicks", type=int, default=1, help="Number of clicks.")
    parser.add_argument("--amount", type=int, help="Scroll ticks (positive up, negative down).")
    parser.add_argument("--text", help="Text string to type.")
    parser.add_argument("--key", help="Key or hotkey to press (e.g. 'ctrl+c').")
    parser.add_argument("--radius", type=float, help="Radius of the circle (for draw_circle).")
    parser.add_argument("--times", type=int, default=1, help="Repeat count (for alt_tab).")
    parser.add_argument("--reverse", action="store_true", help="Alt+Shift+Tab instead of Alt+Tab (for alt_tab).")
    parser.add_argument("--curviness", type=float, default=0.35, help="Bezier curve shape offset ratio.")
    parser.add_argument("--duration", type=float, help="Duration of mouse movement in seconds.")
    
    # Element query parameters (live AT-SPI resolution)
    parser.add_argument("--app", default=None, help="Application name substring match.")
    parser.add_argument("--title", default=None, help="Window title substring match.")
    parser.add_argument("--label", default=None, help="Case-insensitive substring match on element label.")
    parser.add_argument("--role", default=None, help="AT-SPI role or Skelo type, e.g. 'button'.")
    parser.add_argument("--element-id", default=None, dest="element_id", help="Exact el_N element ID.")
    parser.add_argument("--index", type=int, default=0, help="Candidate index to select (default 0).")
    
    # Click behavior / resolution settings
    parser.add_argument("--click", action="store_true", help="Perform click after resolving element.")
    parser.add_argument("--method", default="cursor", choices=["cursor", "action", "auto"], help="Click execution method.")
    parser.add_argument("--click-point", default="center-jitter", choices=["center", "center-jitter"], dest="click_point")
    parser.add_argument("--retries", type=int, default=3, help="Resolution retries.")
    parser.add_argument("--retry-delay", type=float, default=0.6, dest="retry_delay", help="Seconds between retries.")
    parser.add_argument("--max-depth", type=int, default=30, dest="max_depth", help="Max AT-SPI tree depth.")
    parser.add_argument("--max-elements", type=int, default=5000, dest="max_elements", help="Max AT-SPI tree element count.")
    parser.add_argument("--no-activate", action="store_true", dest="no_activate", help="Skip window activation.")
    parser.add_argument("--confirm", action="store_true", help="Verify state change after action.")
    parser.add_argument("--force", action="store_true",
                         help="Bypass the topmost-window safety gate for type/keypress/scroll/drag/"
                              "unlabeled click. Only use this if you've already confirmed the "
                              "right window has real OS focus some other way.")
    parser.add_argument("--list-candidates", action="store_true", dest="list_candidates", help="Just list matching candidates.")
    
    # Sequence options
    parser.add_argument("--sequence", help="JSON string: list of step/action objects.")
    parser.add_argument("--file", help="Path to a JSON file containing a list of step/action objects.")
    
    args = parser.parse_args()

    # Load pyautogui early
    global pyautogui
    pyautogui, _pag_err = load_pyautogui()
    if pyautogui is None:
        print(json.dumps(_pag_err))
        sys.exit(1)

    # 1. Sequence mode
    if args.sequence or args.file:
        actions_to_run = []
        if args.sequence:
            try:
                actions_to_run = json.loads(args.sequence)
            except Exception as e:
                print(json.dumps({"status": "error", "error": f"Failed to parse --sequence JSON: {e}"}))
                sys.exit(1)
        elif args.file:
            if not os.path.exists(args.file):
                print(json.dumps({"status": "error", "error": f"File '{args.file}' not found"}))
                sys.exit(1)
            try:
                with open(args.file, "r") as f:
                    actions_to_run = json.load(f)
            except Exception as e:
                print(json.dumps({"status": "error", "error": f"Failed to parse file JSON: {e}"}))
                sys.exit(1)

        if not isinstance(actions_to_run, list):
            print(json.dumps({"status": "error", "error": "Sequence must be a list of action/step objects"}))
            sys.exit(1)

        result = run_sequence(actions_to_run)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("status") == "success" else 1)

    # 2. Single action mode
    # Convert CLI arguments to a dictionary for run_single_action
    act_dict = {
        "action": args.action,
        "x": args.x,
        "y": args.y,
        "x2": args.x2,
        "y2": args.y2,
        "button": args.button,
        "clicks": args.clicks,
        "amount": args.amount,
        "text": args.text,
        "key": args.key,
        "radius": args.radius,
        "curviness": args.curviness,
        "duration": args.duration,
        "times": args.times,
        "reverse": args.reverse,

        "app": args.app,
        "title": args.title,
        "label": args.label,
        "role": args.role,
        "element_id": args.element_id,
        "index": args.index,
        
        "click": args.click,
        "method": args.method,
        "click_point": args.click_point,
        "retries": args.retries,
        "retry_delay": args.retry_delay,
        "max_depth": args.max_depth,
        "max_elements": args.max_elements,
        "no_activate": args.no_activate,
        "confirm": args.confirm,
        "force": args.force,
        "list_candidates": args.list_candidates,
    }
    
    # Filter out None values to let defaults apply
    act_dict = {k: v for k, v in act_dict.items() if v is not None}

    # Validate action
    if not args.action and not any(k in act_dict for k in ("app", "title", "label", "role", "element_id")):
        parser.error("Must specify --action, --sequence, --file, or element query parameters")

    try:
        result = run_single_action(act_dict)
        if "error" in result and not result.get("status") == "success":
            result["status"] = "error"
            print(json.dumps(result, indent=2))
            sys.exit(1)
            
        result["status"] = "success"
        result["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print(json.dumps(result, indent=2))
        sys.exit(0)
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
