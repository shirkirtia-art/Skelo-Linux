#!/usr/bin/env python3
"""
Skelo — inspect_window.py

Step 2 of the Skelo Linux workflow: full contents of one chosen window.

Given an application name and/or window title (as reported by
list_windows.py), walks that window's AT-SPI accessibility tree and
returns the *meaningful* elements — buttons, text, labels, list items,
etc. — as a flat, reading-order-sorted JSON list, each with its type,
label/value, exact screen bounds, a coarse on-screen region, and a short
breadcrumb of where it lives in the UI.

Why flat instead of a nested tree by default: native UI trees are full of
layout-only wrapper nodes (unlabeled panels, scroll areas, filler
containers) that carry no information an agent needs. This script prunes
those out of the *output* (it still walks through them to reach their
children) and returns a flat list sorted top-to-bottom, left-to-right —
the same order a person would read the screen.

Usage:
    python3 inspect_window.py --app "Spotify"
    python3 inspect_window.py --app "Spotify" --title "Premium"
    python3 inspect_window.py --title "Google Chrome" --interactive-only
    python3 inspect_window.py --app "Spotify" --format tree   # legacy full tree

Output (default, --format flat): JSON to stdout, e.g.

{
  "timestamp": "...",
  "app_name": "Spotify",
  "window_title": "Spotify Premium",
  "window_bounds": {"x": 0, "y": 0, "width": 1200, "height": 800},
  "visited_count": 812,
  "element_count": 96,
  "truncated": false,
  "elements": [
    {
      "id": "el_1",
      "type": "list_item",
      "label": "Song Name - Artist",
      "bounds": {"x": 40, "y": 120, "width": 380, "height": 32},
      "region": "top-left",
      "path": "Sidebar > Playlist",
      "clickable": true,
      "click_target": {"x": 230, "y": 136}
    }
  ]
}

`click_target` is the pre-computed center point of an element's bounds,
included for anything clickable plus text fields/combo boxes/tabs/
sliders/spinners (things normally clicked to focus/activate even if not
flagged "clickable").

`label_inferred: true` means the label was borrowed from the element's
description or a labeled child (common for icon-only buttons) rather
than the element's own name — a best-effort guess, not ground truth.

`visited_count` is how many AT-SPI nodes were walked in total;
`element_count` is how many made it into the pruned `elements` list.

IMPORTANT — this is a snapshot: if you're about to click something,
prefer skelo_action.py over reusing these bounds/click_target values.
The window may move, scroll, or re-render between this call and your
next action; skelo_action.py always re-walks the live tree immediately
before acting, so it can't go stale the way a cached coordinate can.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skelo_common import (
    ATSPI_AVAILABLE, atspi_unavailable_error,
    TOP_LEVEL_ROLES, ROLE_TYPE_MAP, CLICKABLE_ROLES, STRUCTURAL_ROLES,
    get_bounds, is_state, resolve_label, classify_region, find_window,
    is_clickable, is_meaningful_element, get_value,
)

if ATSPI_AVAILABLE:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
else:
    print(json.dumps(atspi_unavailable_error()))
    sys.exit(1)


class FlatWalker:
    """Walks the full AT-SPI tree but only emits *meaningful* nodes
    (labeled, valued, interactive, focused, or selected) into a flat
    list. Pure layout wrappers are traversed through, not emitted."""

    MAX_PATH_DEPTH = 5

    def __init__(self, max_depth, max_elements, interactive_only, max_value_len):
        self.max_depth = max_depth
        self.max_elements = max_elements
        self.interactive_only = interactive_only
        self.max_value_len = max_value_len
        self.visited = 0
        self.truncated = False
        self.elements = []

    def walk(self, acc, depth, path, window_bounds):
        if acc is None:
            return
        if depth > self.max_depth or self.visited >= self.max_elements:
            self.truncated = True
            return

        self.visited += 1

        try:
            role = acc.get_role_name()
        except Exception:
            role = "unknown"

        elem_type = ROLE_TYPE_MAP.get(role, role.replace(" ", "_") if role else "unknown")
        label, label_inferred = resolve_label(acc, role)
        bounds = get_bounds(acc)
        value = get_value(acc, self.max_value_len)
        clickable = is_clickable(acc, role)
        focused = is_state(acc, Atspi.StateType.FOCUSED)
        selected = is_state(acc, Atspi.StateType.SELECTED)

        has_area = bool(bounds) and bounds["width"] > 0 and bounds["height"] > 0
        meaningful = is_meaningful_element(
            elem_type, role, label, value, bounds, clickable, focused, selected,
            interactive_only=self.interactive_only,
        )

        if meaningful:
            node = {
                "id": f"el_{len(self.elements) + 1}",
                "type": elem_type,
                "label": label,
                "bounds": bounds,
            }
            if label_inferred:
                node["label_inferred"] = True
            region = classify_region(bounds, window_bounds)
            if region:
                node["region"] = region
            if value not in (None, ""):
                node["value"] = value
            if clickable is not None:
                node["clickable"] = clickable
            if bounds and (clickable or elem_type in
                            ("text_field", "combo_box", "tab", "spinner", "slider")):
                node["click_target"] = {
                    "x": bounds["x"] + bounds["width"] // 2,
                    "y": bounds["y"] + bounds["height"] // 2,
                }
            if focused:
                node["focused"] = True
            if selected:
                node["selected"] = True
            if path:
                node["path"] = " > ".join(path)
            self.elements.append(node)

        new_path = path + [label] if label else path
        if len(new_path) > self.MAX_PATH_DEPTH:
            new_path = new_path[-self.MAX_PATH_DEPTH:]

        try:
            n_children = acc.get_child_count()
        except Exception:
            n_children = 0

        for i in range(n_children):
            if self.visited >= self.max_elements:
                self.truncated = True
                break
            try:
                child = acc.get_child_at_index(i)
            except Exception:
                continue
            self.walk(child, depth + 1, new_path, window_bounds)


class TreeWalker:
    """Legacy full-tree walker (every node, nested), for --format tree
    when an agent genuinely needs full hierarchy rather than a flat,
    pruned list."""

    def __init__(self, max_depth, max_elements):
        self.max_depth = max_depth
        self.max_elements = max_elements
        self.count = 0
        self.truncated = False

    def walk(self, acc, depth=0):
        if acc is None:
            return None
        if depth > self.max_depth or self.count >= self.max_elements:
            self.truncated = True
            return None

        self.count += 1
        try:
            role = acc.get_role_name()
        except Exception:
            role = "unknown"

        elem_type = ROLE_TYPE_MAP.get(role, role.replace(" ", "_") if role else "unknown")
        label, label_inferred = resolve_label(acc, role)

        node = {"type": elem_type, "id": f"el_{self.count}", "label": label}
        if label_inferred:
            node["label_inferred"] = True

        bounds = get_bounds(acc)
        if bounds:
            node["bounds"] = bounds

        value = get_value(acc)
        if value:
            node["value"] = value

        clickable = is_clickable(acc, role)
        if clickable is not None:
            node["clickable"] = clickable

        if bounds and (clickable or elem_type in
                        ("text_field", "combo_box", "tab", "spinner", "slider")):
            node["click_target"] = {
                "x": bounds["x"] + bounds["width"] // 2,
                "y": bounds["y"] + bounds["height"] // 2,
            }

        if is_state(acc, Atspi.StateType.FOCUSED):
            node["focused"] = True
        if is_state(acc, Atspi.StateType.SELECTED):
            node["selected"] = True

        children_out = []
        try:
            n_children = acc.get_child_count()
        except Exception:
            n_children = 0

        for i in range(n_children):
            if self.count >= self.max_elements:
                self.truncated = True
                break
            try:
                child = acc.get_child_at_index(i)
            except Exception:
                continue
            child_node = self.walk(child, depth + 1)
            if child_node:
                children_out.append(child_node)

        if children_out:
            node["children"] = children_out

        return node


def main():
    parser = argparse.ArgumentParser(description="Dump meaningful AT-SPI UI elements for a window")
    parser.add_argument("--app", default=None, help="App name substring, e.g. 'spotify' (case-insensitive)")
    parser.add_argument("--title", default=None, help="Window title substring (case-insensitive)")
    parser.add_argument("--max-depth", type=int, default=30, help="Max tree depth to walk")
    parser.add_argument("--max-elements", type=int, default=5000, help="Safety cap on total nodes visited")
    parser.add_argument("--format", choices=["flat", "tree"], default="flat",
                         help="'flat' (default): pruned, reading-order list with region/path context. "
                              "'tree': legacy full nested tree, every node included.")
    parser.add_argument("--interactive-only", action="store_true",
                         help="Flat mode only: keep just clickable/editable/focused/selected elements.")
    parser.add_argument("--max-value-len", type=int, default=300,
                         help="Truncate text/value content to this many characters (flat mode).")
    args = parser.parse_args()

    if not args.app and not args.title:
        print(json.dumps({"error": "Provide at least one of --app or --title"}))
        sys.exit(1)

    app, win = find_window(args.app, args.title)
    if win is None:
        print(json.dumps({
            "error": "No matching window found",
            "app_query": args.app,
            "title_query": args.title,
            "hint": "Run list_windows.py first to see exact app_name/window_title values",
        }))
        sys.exit(1)

    window_bounds = get_bounds(win)

    if args.format == "tree":
        walker = TreeWalker(args.max_depth, args.max_elements)
        tree = walker.walk(win)
        result = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "app_name": app.get_name(),
            "window_title": win.get_name(),
            "window_bounds": window_bounds,
            "element_count": walker.count,
            "truncated": walker.truncated,
            "ui_tree": tree,
        }
    else:
        walker = FlatWalker(args.max_depth, args.max_elements, args.interactive_only, args.max_value_len)
        walker.walk(win, 0, [], window_bounds)
        elements = sorted(
            walker.elements,
            key=lambda e: (e["bounds"]["y"], e["bounds"]["x"]) if e.get("bounds") else (0, 0),
        )
        result = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "app_name": app.get_name(),
            "window_title": win.get_name(),
            "window_bounds": window_bounds,
            "visited_count": walker.visited,
            "element_count": len(elements),
            "truncated": walker.truncated,
            "elements": elements,
        }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()