"""
Skelo Desktop SDK — high-level programmatic interface for AI agents.
"""

import time
import sys
import os

# Ensure repository root is on sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from list_windows import list_windows as _raw_list_windows
from inspect_window import FlatWalker
from app_launcher import get_apps_categorized, launch_app_by_style
import skelo_common
from skelo_common import (
    find_window, get_bounds, ensure_window_topmost, window_manage,
    get_screen_resolution
)
from skelo_action import (
    search_element, action_click, action_type, action_keypress,
    action_scroll, action_drag, run_single_action, direct_action_click
)


class Element:
    """Represents a concrete interactive UI control in a window."""

    def __init__(self, node_dict, window=None, acc=None):
        self._node = node_dict
        self._window = window
        self._acc = acc

        self.id = node_dict.get("id")
        self.type = node_dict.get("type")
        self.label = node_dict.get("label")
        self.role = node_dict.get("role")
        self.bounds = node_dict.get("bounds")
        self.clickable = node_dict.get("clickable", False)
        self.click_target = node_dict.get("click_target")
        self.region = node_dict.get("region")
        self.value = node_dict.get("value")
        self.path = node_dict.get("path")

    def click(self, button="left", clicks=1, method="auto"):
        """Click this element directly."""
        if method in ("action", "auto") and self._acc:
            ok, err = direct_action_click(self._acc)
            if ok:
                return {"clicked": True, "method": "action"}
        if self.click_target:
            return action_click(self.click_target["x"], self.click_target["y"], button=button, clicks=clicks)
        elif self.bounds:
            cx = self.bounds["x"] + self.bounds["width"] // 2
            cy = self.bounds["y"] + self.bounds["height"] // 2
            return action_click(cx, cy, button=button, clicks=clicks)
        raise RuntimeError(f"Element {self.id} ({self.label}) has no valid screen coordinates or action interface")

    def to_dict(self):
        return dict(self._node)

    def __repr__(self):
        return f"<Element id='{self.id}' type='{self.type}' label='{self.label}' region='{self.region}'>"


class Window:
    """Represents an active application window on the desktop."""

    def __init__(self, win_dict, app_acc=None, win_acc=None):
        self.app_name = win_dict.get("app_name")
        self.title = win_dict.get("window_title")
        self.pid = win_dict.get("process_id")
        self.role = win_dict.get("role")
        self.bounds = win_dict.get("bounds")
        self.is_focused = win_dict.get("is_focused", False)
        self.source = win_dict.get("source", "atspi")
        self._app_acc = app_acc
        self._win_acc = win_acc

    def _ensure_acc(self):
        if self._win_acc is None and self.app_name:
            self._app_acc, self._win_acc = find_window(self.app_name, self.title)
        return self._win_acc

    def raise_window(self):
        """Bring this window to the front and verify topmost status."""
        win_acc = self._ensure_acc()
        ok, err = window_manage(self.app_name, "raise", title_query=self.title)
        if win_acc:
            ensure_window_topmost(win_acc)
        return ok

    def minimize(self):
        ok, err = window_manage(self.app_name, "minimize", title_query=self.title)
        return ok

    def maximize(self):
        ok, err = window_manage(self.app_name, "maximize", title_query=self.title)
        return ok

    def unmaximize(self):
        ok, err = window_manage(self.app_name, "unmaximize", title_query=self.title)
        return ok

    def close(self):
        ok, err = window_manage(self.app_name, "close", title_query=self.title)
        return ok

    def inspect(self, interactive_only=False, max_depth=30, max_elements=5000):
        """Dump all labeled UI elements inside this window."""
        win_acc = self._ensure_acc()
        if win_acc is None:
            return []
        win_bounds = get_bounds(win_acc) or self.bounds
        walker = FlatWalker(max_depth=max_depth, max_elements=max_elements,
                            interactive_only=interactive_only, max_value_len=300)
        walker.walk(win_acc, 0, [], win_bounds)
        elements = sorted(
            walker.elements,
            key=lambda e: (e["bounds"]["y"], e["bounds"]["x"]) if e.get("bounds") else (0, 0),
        )
        out = []
        for idx, el in enumerate(elements, 1):
            el["id"] = f"el_{idx}"
            out.append(Element(el, window=self))
        return out

    def find_element(self, label=None, role=None, element_id=None):
        """Find an interactive element by label, role, or sequential ID."""
        win_acc = self._ensure_acc()
        if win_acc is None:
            return None
        candidates = search_element(win_acc, label=label, role=role, element_id=element_id)
        if not candidates:
            return None
        pick = candidates[0]
        return Element(pick["node"], window=self, acc=pick["acc"])

    def wait_for_element(self, label=None, role=None, timeout=10.0, poll_interval=0.25):
        """Poll until a matching element appears in this window, or raise TimeoutError."""
        start = time.time()
        while time.time() - start < timeout:
            el = self.find_element(label=label, role=role)
            if el:
                return el
            time.sleep(poll_interval)
        raise TimeoutError(f"Element label='{label}' role='{role}' did not appear in window '{self.title}' within {timeout}s")

    def click(self, label=None, role=None, element_id=None, button="left", clicks=1, method="cursor"):
        """Click an element in this window by label or ID."""
        self.raise_window()
        act_dict = {
            "action": "click",
            "app": self.app_name,
            "title": self.title,
            "label": label,
            "role": role,
            "element_id": element_id,
            "button": button,
            "clicks": clicks,
            "method": method,
        }
        return run_single_action(act_dict)

    def type(self, text, label=None):
        """Type text into this window (optionally focusing an element first)."""
        self.raise_window()
        act_dict = {
            "action": "type",
            "app": self.app_name,
            "title": self.title,
            "label": label,
            "text": text,
        }
        return run_single_action(act_dict)

    def key(self, hotkey):
        """Press a key or hotkey combo (e.g. 'ctrl+c') in this window."""
        self.raise_window()
        return action_keypress(hotkey)

    def to_dict(self):
        return {
            "app_name": self.app_name,
            "window_title": self.title,
            "process_id": self.pid,
            "role": self.role,
            "bounds": self.bounds,
            "is_focused": self.is_focused,
            "source": self.source,
        }

    def __repr__(self):
        return f"<Window app='{self.app_name}' title='{self.title}' pid={self.pid}>"


class Desktop:
    """Entry point for interacting with the Linux desktop environment."""

    def __init__(self):
        pass

    @property
    def resolution(self):
        return get_screen_resolution()

    def list_windows(self, include_all=False):
        """Returns a list of Window objects for all currently open windows."""
        windows_data, _warning = _raw_list_windows(include_all=include_all)
        return [Window(w) for w in windows_data]

    def get_window(self, app=None, title=None):
        """Find the first open window matching the given app or title query."""
        windows = self.list_windows()
        q_app = (app or "").lower()
        q_title = (title or "").lower()

        for w in windows:
            app_match = not q_app or q_app in (w.app_name or "").lower()
            title_match = not q_title or q_title in (w.title or "").lower()
            if app_match and title_match:
                return w
        return None

    def wait_for_window(self, app=None, title=None, timeout=10.0, poll_interval=0.3):
        """Poll until a matching window opens, or raise TimeoutError."""
        start = time.time()
        while time.time() - start < timeout:
            w = self.get_window(app=app, title=title)
            if w:
                return w
            time.sleep(poll_interval)
        raise TimeoutError(f"Window matching app='{app}' title='{title}' did not open within {timeout}s")

    def list_apps(self):
        """Returns a dict of all installed applications categorized with index numbers."""
        indexed_apps, categorized = get_apps_categorized()
        return indexed_apps

    def open_app(self, name_or_index):
        """Launch an installed application by index or name."""
        indexed_apps, categorized = get_apps_categorized()
        target = None
        query = str(name_or_index).strip()

        if query.isdigit():
            target = indexed_apps.get(int(query))
        else:
            q_low = query.lower()
            exact = [a for a in indexed_apps.values() if a["name"].lower() == q_low]
            target = exact[0] if exact else None
            if not target:
                substr = [a for a in indexed_apps.values() if q_low in a["name"].lower()]
                target = substr[0] if substr else None

        if not target:
            raise ValueError(f"No installed application found matching '{name_or_index}'")

        ok, err = launch_app_by_style(target)
        if not ok:
            raise RuntimeError(f"Failed to launch '{target['name']}': {err}")
        return target
