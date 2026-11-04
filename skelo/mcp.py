"""
Skelo Model Context Protocol (MCP) Server.

High-performance, native Model Context Protocol (MCP) server providing
line-delimited JSON-RPC 2.0 communication over standard I/O for AI agents
(Claude Desktop, Antigravity, Cursor, LangChain).
"""

import copy
import json
import os
import shutil
import sys
import time
import traceback

# Ensure repository root is on sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from list_windows import list_windows
from inspect_window import FlatWalker
from app_launcher import get_apps_categorized, launch_app_by_style
from skelo_common import (
    ATSPI_AVAILABLE, find_window, get_bounds, get_screen_resolution
)
from skelo_action import (
    search_element, run_single_action, run_sequence, action_keypress
)
from skelo.desktop import Desktop

if ATSPI_AVAILABLE:
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception:
        Atspi = None
else:
    Atspi = None


class SkeloMCPServer:
    """
    Zero-dependency Model Context Protocol (MCP) Server for Skelo-Linux.
    Reads line-delimited JSON-RPC 2.0 messages from stdin and writes responses to stdout.
    All logging/diagnostic output is isolated strictly to stderr.
    """

    PROTOCOL_VERSION = "2024-11-05"
    SERVER_NAME = "skelo-linux"
    SERVER_VERSION = "1.1.0"

    def __init__(self):
        self._real_stdout = sys.stdout
        self._running = False
        self._last_app = None
        self._last_title = None
        self._tool_handlers = {
            "skelo_list_windows": self._tool_list_windows,
            "list_windows": self._tool_list_windows,
            "skelo_inspect": self._tool_inspect,
            "inspect_window": self._tool_inspect,
            "inspect": self._tool_inspect,
            "skelo_click": self._tool_click,
            "click": self._tool_click,
            "skelo_type": self._tool_type,
            "type_text": self._tool_type,
            "type": self._tool_type,
            "skelo_hotkey": self._tool_hotkey,
            "hotkey": self._tool_hotkey,
            "skelo_wait": self._tool_wait,
            "wait_for": self._tool_wait,
            "wait": self._tool_wait,
            "skelo_batch": self._tool_batch,
            "batch": self._tool_batch,
            "skelo_launch": self._tool_launch,
            "launch_app": self._tool_launch,
            "launch": self._tool_launch,
        }

    def _log(self, message):
        """Write debug/log messages strictly to stderr."""
        sys.stderr.write(f"[skelo-mcp] {message}\n")
        sys.stderr.flush()

    def _send_response(self, response_dict):
        """Write single-line JSON-RPC 2.0 response to real stdout."""
        payload = json.dumps(response_dict, separators=(",", ":"))
        self._real_stdout.write(payload + "\n")
        self._real_stdout.flush()

    def _get_tools_spec(self):
        """Returns the list of MCP tool definitions with inputSchemas."""
        return [
            {
                "name": "skelo_list_windows",
                "description": "List all open desktop windows with app names, window titles, process IDs, focus status, and bounding coordinates.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "detailed": {
                            "type": "boolean",
                            "description": "If true, show all raw windows without deduplication or subframe filtering."
                        }
                    }
                }
            },
            {
                "name": "skelo_inspect",
                "description": "Inspect and dump UI elements (buttons, inputs, tabs, menus, text) inside a window using AT-SPI.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "app": {
                            "type": "string",
                            "description": "Application name substring (case-insensitive)"
                        },
                        "title": {
                            "type": "string",
                            "description": "Window title substring (case-insensitive)"
                        },
                        "compact": {
                            "type": "boolean",
                            "default": True,
                            "description": "If true (default), returns a concise representation omitting internal layout nodes."
                        },
                        "interactive_only": {
                            "type": "boolean",
                            "default": False,
                            "description": "If true, only returns interactive/clickable/editable elements."
                        }
                    }
                }
            },
            {
                "name": "skelo_click",
                "description": "Click an interactive element in a window by label, role, element_id, or coordinate (x, y).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "app": {
                            "type": "string",
                            "description": "Application name substring"
                        },
                        "label": {
                            "type": "string",
                            "description": "Element label/name to match"
                        },
                        "role": {
                            "type": "string",
                            "description": "AT-SPI role to match"
                        },
                        "element_id": {
                            "type": "string",
                            "description": "Sequential ID from skelo_inspect (e.g. 'el_5')"
                        },
                        "x": {
                            "type": "number",
                            "description": "Screen X coordinate for raw click"
                        },
                        "y": {
                            "type": "number",
                            "description": "Screen Y coordinate for raw click"
                        },
                        "button": {
                            "type": "string",
                            "enum": ["left", "right", "middle"],
                            "default": "left",
                            "description": "Mouse button to click"
                        },
                        "clicks": {
                            "type": "integer",
                            "default": 1,
                            "description": "Number of clicks (1 for single, 2 for double click)"
                        }
                    }
                }
            },
            {
                "name": "skelo_type",
                "description": "Type text into the desktop, optionally focusing an application or specific element first.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Text to type"
                        },
                        "app": {
                            "type": "string",
                            "description": "Application name substring"
                        },
                        "element_id": {
                            "type": "string",
                            "description": "Element ID to focus before typing"
                        },
                        "press_enter": {
                            "type": "boolean",
                            "default": False,
                            "description": "Whether to press Enter after typing"
                        }
                    },
                    "required": ["text"]
                }
            },
            {
                "name": "skelo_hotkey",
                "description": "Press a keyboard shortcut or key combination (e.g. 'ctrl+s', 'Return', 'alt+tab').",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Key or hotkey combo (e.g. 'ctrl+s', 'Return', 'alt+tab')"
                        }
                    },
                    "required": ["key"]
                }
            },
            {
                "name": "skelo_wait",
                "description": "Wait for an application window or UI element to appear (or disappear) within a timeout.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "app": {
                            "type": "string",
                            "description": "Application name substring"
                        },
                        "title": {
                            "type": "string",
                            "description": "Window title substring"
                        },
                        "label": {
                            "type": "string",
                            "description": "Element label to wait for"
                        },
                        "role": {
                            "type": "string",
                            "description": "Element role to wait for"
                        },
                        "element_id": {
                            "type": "string",
                            "description": "Element ID to wait for"
                        },
                        "timeout": {
                            "type": "number",
                            "default": 5.0,
                            "description": "Maximum seconds to wait (default 5.0)"
                        },
                        "disappear": {
                            "type": "boolean",
                            "default": False,
                            "description": "If true, wait until the window/element disappears"
                        }
                    }
                }
            },
            {
                "name": "skelo_batch",
                "description": "Execute a sequence of Skelo actions in a single batch (e.g. click, type, hotkey, sleep).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "description": "List of action dictionaries to execute in order",
                            "items": {
                                "type": "object"
                            }
                        }
                    },
                    "required": ["actions"]
                }
            },
            {
                "name": "skelo_launch",
                "description": "Launch an installed desktop application by name or desktop index number, optionally waiting for its window to open.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "app": {
                            "type": "string",
                            "description": "Application name substring (e.g. 'Spotify', 'Chrome') or index number"
                        },
                        "wait": {
                            "type": "boolean",
                            "default": True,
                            "description": "Whether to wait for the application window to open before returning"
                        }
                    },
                    "required": ["app"]
                }
            }
        ]

    # --------------------------------------------------------------------------
    # Tool Implementations
    # --------------------------------------------------------------------------

    def _tool_list_windows(self, arguments):
        detailed = bool(arguments.get("detailed", False))
        windows, err = list_windows(include_all=detailed)
        result = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "screen_resolution": get_screen_resolution(),
            "window_count": len(windows),
            "windows": windows,
        }
        if err:
            result["warning"] = f"Desktop enumeration issue: {err}"
        return result

    def _tool_inspect(self, arguments):
        app_query = arguments.get("app")
        title_query = arguments.get("title")
        compact = arguments.get("compact", True)
        interactive_only = arguments.get("interactive_only", False)

        if not app_query and not title_query:
            raise ValueError("Provide at least one of 'app' or 'title' to inspect")

        app, win = find_window(app_query, title_query)
        if win is None:
            raise ValueError(f"No matching window found for app='{app_query}', title='{title_query}'")

        window_bounds = get_bounds(win)
        walker = FlatWalker(
            max_depth=30,
            max_elements=5000,
            interactive_only=interactive_only,
            max_value_len=300
        )
        walker.walk(win, 0, [], window_bounds)
        elements = sorted(
            walker.elements,
            key=lambda e: (e["bounds"]["y"], e["bounds"]["x"]) if e.get("bounds") else (0, 0),
        )
        for idx, el in enumerate(elements, 1):
            el["id"] = f"el_{idx}"

        if compact:
            compact_elements = []
            for el in elements:
                c = {
                    "id": el.get("id"),
                    "type": el.get("type"),
                    "role": el.get("role"),
                    "label": el.get("label"),
                    "clickable": el.get("clickable", False),
                }
                if el.get("value") is not None:
                    c["value"] = el.get("value")
                if el.get("click_target"):
                    c["click_target"] = el.get("click_target")
                if el.get("bounds"):
                    c["bounds"] = el.get("bounds")
                if el.get("region"):
                    c["region"] = el.get("region")
                compact_elements.append(c)
            elements = compact_elements

        app_name = app.get_name() if hasattr(app, "get_name") else str(app)
        win_name = win.get_name() if hasattr(win, "get_name") else str(win)
        self._last_app = app_name
        self._last_title = win_name

        return {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "app_name": app_name,
            "window_title": win_name,
            "window_bounds": window_bounds,
            "visited_count": walker.visited,
            "element_count": len(elements),
            "truncated": walker.truncated,
            "elements": elements,
        }

    def _tool_click(self, arguments):
        act_dict = {"action": "click"}
        for key in ("app", "label", "role", "element_id", "x", "y"):
            if key in arguments and arguments[key] is not None:
                act_dict[key] = arguments[key]
        if "app" not in act_dict and "title" not in act_dict and self._last_app:
            act_dict["app"] = self._last_app
            if self._last_title:
                act_dict["title"] = self._last_title
        act_dict["button"] = arguments.get("button", "left")
        act_dict["clicks"] = int(arguments.get("clicks", 1))

        has_target = any(k in act_dict for k in ("label", "role", "element_id", "x", "y"))
        if not has_target:
            raise ValueError(
                "skelo_click requires either coordinates ('x', 'y') or an element specifier "
                "('label', 'role', or 'element_id')"
            )

        res = run_single_action(act_dict)
        if isinstance(res, dict) and (res.get("status") == "error" or res.get("error")):
            raise RuntimeError(res.get("error", "Click action failed"))
        return res

    def _tool_type(self, arguments):
        text = arguments.get("text")
        if text is None:
            raise ValueError("'text' argument is required for skelo_type")

        act_dict = {"action": "type", "text": str(text)}
        if arguments.get("app"):
            act_dict["app"] = arguments["app"]
        elif self._last_app:
            act_dict["app"] = self._last_app
        if arguments.get("title"):
            act_dict["title"] = arguments["title"]
        elif self._last_title and not arguments.get("app"):
            act_dict["title"] = self._last_title
        if arguments.get("element_id"):
            act_dict["element_id"] = arguments["element_id"]

        res = run_single_action(act_dict)
        if isinstance(res, dict) and (res.get("status") == "error" or res.get("error")):
            raise RuntimeError(res.get("error", "Type action failed"))

        if arguments.get("press_enter", False):
            time.sleep(0.05)
            action_keypress("Return")
            if isinstance(res, dict):
                res["pressed_enter"] = True

        return res

    def _tool_hotkey(self, arguments):
        key = arguments.get("key")
        if not key:
            raise ValueError("'key' argument is required for skelo_hotkey")
        res = action_keypress(str(key))
        return res

    def _tool_wait(self, arguments):
        app = arguments.get("app")
        title = arguments.get("title")
        label = arguments.get("label")
        role = arguments.get("role")
        element_id = arguments.get("element_id")
        timeout = float(arguments.get("timeout", 5.0))
        disappear = bool(arguments.get("disappear", False))

        if not any([app, title, label, role, element_id]):
            raise ValueError("At least one of 'app', 'title', 'label', 'role', or 'element_id' must be specified for skelo_wait")

        start = time.time()
        poll_interval = 0.25
        is_element_wait = bool(label or role or element_id)

        while True:
            now = time.time()
            elapsed = round(now - start, 2)

            if is_element_wait:
                _app_acc, win_acc = find_window(app, title) if (app or title) else (None, None)
                element_found = False
                elem_info = None

                if win_acc is not None:
                    candidates = search_element(win_acc, label=label, role=role, element_id=element_id)
                    element_found = len(candidates) > 0
                    elem_info = candidates[0]["node"] if element_found else None
                elif not (app or title) and Atspi:
                    desktop = Atspi.get_desktop(0)
                    app_count = desktop.get_child_count() if desktop else 0
                    for i in range(app_count):
                        a = desktop.get_child_at_index(i)
                        if not a:
                            continue
                        for j in range(a.get_child_count()):
                            w = a.get_child_at_index(j)
                            if not w:
                                continue
                            c = search_element(w, label=label, role=role, element_id=element_id)
                            if c:
                                element_found = True
                                elem_info = c[0]["node"]
                                break
                        if element_found:
                            break

                if not disappear:
                    if element_found:
                        return {
                            "status": "success",
                            "condition": "appeared",
                            "elapsed_seconds": elapsed,
                            "element": elem_info
                        }
                else:
                    if not element_found:
                        return {
                            "status": "success",
                            "condition": "disappeared",
                            "elapsed_seconds": elapsed
                        }
            else:
                desktop = Desktop()
                w = desktop.get_window(app=app, title=title)
                window_found = (w is not None)

                if not disappear:
                    if window_found:
                        return {
                            "status": "success",
                            "condition": "appeared",
                            "elapsed_seconds": elapsed,
                            "window": w.to_dict()
                        }
                else:
                    if not window_found:
                        return {
                            "status": "success",
                            "condition": "disappeared",
                            "elapsed_seconds": elapsed
                        }

            if now - start >= timeout:
                target_name = label or role or element_id or title or app
                cond_str = "disappear" if disappear else "appear"
                raise TimeoutError(f"Timed out waiting for '{target_name}' to {cond_str} after {timeout:.1f}s")

            time.sleep(poll_interval)

    def _tool_batch(self, arguments):
        actions = arguments.get("actions")
        if not isinstance(actions, list):
            raise ValueError("'actions' argument must be an array of action objects")
        steps = copy.deepcopy(actions)
        for step in steps:
            if isinstance(step, dict) and "app" not in step and "title" not in step and self._last_app:
                # If step targets an element or needs window context, inherit last inspected window
                if any(k in step for k in ("label", "role", "element_id")) or step.get("action") in ("click", "type"):
                    step["app"] = self._last_app
                    if self._last_title:
                        step["title"] = self._last_title
        res = run_sequence(steps)
        if isinstance(res, dict) and res.get("status") == "error":
            err_msg = res.get("error") or f"Action failed at step {res.get('stopped_at_step')}"
            raise RuntimeError(f"{err_msg}: {json.dumps(res)}")
        return res

    def _tool_launch(self, arguments):
        app_arg = arguments.get("app")
        if not app_arg:
            raise ValueError("'app' argument is required for skelo_launch")
        wait = arguments.get("wait", True)

        desktop = Desktop()
        query = str(app_arg).strip()

        try:
            target = desktop.open_app(query)
        except ValueError:
            if shutil.which(query):
                target = {"name": query, "exec": query, "category": "Direct Binary", "index": None}
                ok, err = launch_app_by_style(target)
                if not ok:
                    raise RuntimeError(f"Failed to launch binary '{query}': {err}")
            else:
                raise

        res = {
            "status": "success",
            "launched": target.get("name"),
            "index": target.get("index"),
            "exec": target.get("exec"),
        }

        if wait:
            poll_start = time.time()
            opened_win = None
            app_query = target.get("name")
            while time.time() - poll_start < 8.0:
                opened_win = desktop.get_window(app=app_query)
                if opened_win:
                    break
                time.sleep(0.35)

            if opened_win:
                res["window_opened"] = True
                res["window"] = opened_win.to_dict()
            else:
                res["window_opened"] = False
                res["warning"] = f"App '{app_query}' launched, but its window was not detected within 8s"

        return res

    # --------------------------------------------------------------------------
    # Dispatching & JSON-RPC Handling
    # --------------------------------------------------------------------------

    def _dispatch_tool_call(self, name, arguments):
        handler = self._tool_handlers.get(name)
        if not handler:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Error: Unknown tool '{name}'"
                    }
                ],
                "isError": True
            }

        try:
            val = handler(arguments)
            if isinstance(val, (dict, list)):
                text_out = json.dumps(val, indent=2)
            else:
                text_out = str(val)

            return {
                "content": [
                    {
                        "type": "text",
                        "text": text_out
                    }
                ]
            }
        except Exception as e:
            self._log(f"Tool execution failed for '{name}': {e}")
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Error executing tool '{name}': {str(e)}"
                    }
                ],
                "isError": True
            }

    def handle_request(self, req):
        """Process a single JSON-RPC 2.0 request or notification dictionary."""
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
            return {
                "jsonrpc": "2.0",
                "id": req.get("id") if isinstance(req, dict) else None,
                "error": {
                    "code": -32600,
                    "message": "Invalid Request: expected JSON-RPC 2.0 object"
                }
            }

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        is_notification = (req_id is None)

        if method == "initialize":
            result = {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": self.SERVER_NAME,
                    "version": self.SERVER_VERSION
                }
            }
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        elif method in ("notifications/initialized", "initialized"):
            # Acknowledge / no response for notifications
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        elif method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": self._get_tools_spec()
                }
            }

        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments") or {}
            call_result = self._dispatch_tool_call(tool_name, tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": call_result
            }

        elif method == "resources/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

        elif method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

        else:
            if is_notification or (method and method.startswith("notifications/")):
                self._log(f"Ignored unhandled notification '{method}'")
                return None
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: '{method}'"
                }
            }

    def run(self):
        """
        Main loop reading line-delimited JSON-RPC 2.0 from sys.stdin.
        Redirects standard print/stdout to stderr to preserve stdout stream integrity.
        """
        self._running = True
        # Safety: redirect standard sys.stdout to sys.stderr so rogue prints don't corrupt JSON-RPC
        sys.stdout = sys.stderr

        self._log("Skelo MCP Server started. Listening on stdin...")

        try:
            for line in sys.stdin:
                line_str = line.strip()
                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
                except Exception as e:
                    self._send_response({
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32700,
                            "message": f"Parse error: {str(e)}"
                        }
                    })
                    continue

                if isinstance(data, list):
                    # Batch JSON-RPC request
                    responses = []
                    for item in data:
                        resp = self.handle_request(item)
                        if resp is not None:
                            responses.append(resp)
                    if responses:
                        self._send_response(responses)
                else:
                    resp = self.handle_request(data)
                    if resp is not None:
                        self._send_response(resp)

        except (KeyboardInterrupt, SystemExit):
            self._log("Server shutdown requested.")
        except Exception as e:
            self._log(f"Fatal error in MCP server: {traceback.format_exc()}")
        finally:
            self._running = False
            sys.stdout = self._real_stdout
            self._log("Skelo MCP Server stopped.")
