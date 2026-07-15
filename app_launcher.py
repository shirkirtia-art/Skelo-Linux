#!/usr/bin/env python3
"""
Skelo — app_launcher.py

Scans installed desktop applications on the Linux system, categorizes them,
assigns numbers, and allows listing them or launching them by number or name.
"""

import argparse
import glob
import json
import os
import subprocess
import sys


def get_apps():
    """Scans standard desktop entry paths and returns a dictionary of apps."""
    xdg_dirs = os.environ.get('XDG_DATA_DIRS', '/usr/share:/usr/local/share').split(':')
    app_dirs = [
        os.path.expanduser('~/.local/share/applications'),
        '/usr/share/applications',
        '/usr/local/share/applications',
        '/var/lib/flatpak/exports/share/applications',
        '/var/lib/snapd/desktop/applications'
    ]
    for d in xdg_dirs:
        if d:
            app_dirs.append(os.path.join(d, 'applications'))

    # Filter unique, existing directories
    seen_dirs = set()
    unique_dirs = []
    for d in app_dirs:
        abs_d = os.path.abspath(d)
        if abs_d not in seen_dirs and os.path.isdir(abs_d):
            seen_dirs.add(abs_d)
            unique_dirs.append(abs_d)

    apps = {}
    for d in unique_dirs:
        for filepath in glob.glob(os.path.join(d, '*.desktop')):
            try:
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    in_section = False
                    app_name = None
                    exec_cmd = None
                    no_display = False

                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if line.startswith('[') and line.endswith(']'):
                            if line == '[Desktop Entry]':
                                in_section = True
                            else:
                                in_section = False
                            continue
                        if in_section:
                            if '=' in line:
                                key, val = line.split('=', 1)
                                key = key.strip()
                                val = val.strip()
                                if key == 'Name':
                                    app_name = val
                                elif key == 'Exec':
                                    exec_cmd = val
                                elif key == 'NoDisplay' and val.lower() == 'true':
                                    no_display = True

                if app_name and exec_cmd and not no_display:
                    # Clean up Exec line placeholders
                    clean_exec = []
                    for part in exec_cmd.split():
                        if part.startswith('%') and len(part) == 2 and part[1] in 'fFuUick':
                            continue
                        clean_exec.append(part)
                    exec_str = " ".join(clean_exec)

                    # De-duplicate: prefer shorter paths or local overrides
                    existing = apps.get(app_name.lower())
                    if not existing or '/.local/share/' in filepath:
                        apps[app_name.lower()] = {
                            "name": app_name,
                            "exec": exec_str,
                            "desktop_file": filepath
                        }
            except Exception:
                pass

    return apps


def get_apps_categorized():
    """Categorizes the scanned applications and indexes them sequentially."""
    raw_apps = get_apps()
    
    categories = {
        "System Utilities / Native Apps": [],
        "Flatpak Applications": [],
        "Snap Applications": [],
        "AppImage / Local Binaries": [],
        "Cinnamon Settings": [],
        "Administrative Tools (Requires Elevation)": []
    }
    
    for app in sorted(raw_apps.values(), key=lambda a: a["name"].lower()):
        exec_cmd = app["exec"]
        exec_lower = exec_cmd.lower()
        
        if "cinnamon-settings" in exec_lower:
            categories["Cinnamon Settings"].append(app)
        elif "flatpak" in exec_lower:
            categories["Flatpak Applications"].append(app)
        elif "snap" in exec_lower:
            categories["Snap Applications"].append(app)
        elif ".appimage" in exec_lower or "/applications/" in exec_lower:
            categories["AppImage / Local Binaries"].append(app)
        elif "pkexec" in exec_lower or "gksu" in exec_lower or "gksudo" in exec_lower:
            categories["Administrative Tools (Requires Elevation)"].append(app)
        else:
            categories["System Utilities / Native Apps"].append(app)
            
    # Filter empty categories
    categories = {k: v for k, v in categories.items() if v}
    
    indexed_apps = {}
    categorized_with_index = {}
    current_index = 1
    
    for cat_name, app_list in categories.items():
        categorized_with_index[cat_name] = []
        for app in app_list:
            app_copy = app.copy()
            app_copy["index"] = current_index
            app_copy["category"] = cat_name
            indexed_apps[current_index] = app_copy
            categorized_with_index[cat_name].append(app_copy)
            current_index += 1
            
    return indexed_apps, categorized_with_index


def launch_app_by_style(app):
    """Launches the application using category-specific enhancements and clean environments."""
    category = app.get("category", "")
    exec_cmd = app["exec"]
    
    # 1. Build clean environment for the application
    env = os.environ.copy()
    
    # Remove sudo environment variables to prevent Flatpak from rejecting execution
    for key in list(env.keys()):
        if key.startswith("SUDO_"):
            env.pop(key)
            
    # Attempt to locate graphical user info to restore session variables if missing
    try:
        import pwd
        uid = os.getuid()
        username = pwd.getpwuid(uid).pw_name
        
        # If running as root (or through sudo), target ciphyrtech (UID 1000)
        if uid == 0:
            uid = 1000
            username = "ciphyrtech"
    except Exception:
        uid = 1000
        username = "ciphyrtech"

    # Set default graphical environment variables if they are not defined
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"
    if "XAUTHORITY" not in env:
        env["XAUTHORITY"] = f"/home/{username}/.Xauthority"
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

    # 2. Apply style adjustments
    if category == "AppImage / Local Binaries":
        # Ensure executable permissions are set for local binaries/AppImages
        parts = exec_cmd.split()
        if parts:
            path = parts[0].strip('"\'')
            if os.path.exists(path):
                try:
                    os.chmod(path, 0o755)
                except Exception:
                    pass
                    
    elif category == "Flatpak Applications":
        # Remove flatpak forwarding/URI placeholders
        exec_cmd = exec_cmd.replace("@@u @@", "").replace("@@U @@", "").strip()
        
    try:
        subprocess.Popen(
            exec_cmd,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env
        )
        return True, None
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser(description="Application Launcher: Scan and launch applications.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-l", "--list", action="store_true", help="List all scanned applications categorized by number.")
    group.add_argument("-o", "--open", nargs='+', dest="open_app", help="Launch application by name substring or list number.")
    parser.add_argument("--json", action="store_true", help="Output results as JSON.")
    args = parser.parse_args()

    indexed_apps, categorized = get_apps_categorized()

    if args.list:
        if args.json:
            print(json.dumps(indexed_apps, indent=2))
        else:
            for cat_name, app_list in categorized.items():
                print(f"\n=== {cat_name} ===")
                print(f"{'No.':<5} {'Application Name':<40} {'Command/Executable'}")
                print("-" * 85)
                for app in app_list:
                    print(f"[{app['index']}]  {app['name']:<40} {app['exec']}")
            print()
        sys.exit(0)

    if args.open_app:
        # Join potential space-separated words (e.g. 'OBS' 'Studio' -> 'OBS Studio')
        query = " ".join(args.open_app).strip()
        target = None

        # 1. Try numeric lookup first
        if query.isdigit():
            idx = int(query)
            target = indexed_apps.get(idx)
            if not target:
                print(json.dumps({
                    "status": "error",
                    "error": f"Invalid list number: {idx}. Use --list to view valid numbers."
                }, indent=2))
                sys.exit(1)
        else:
            # 2. Try exact name match
            query_lower = query.lower()
            exact_matches = [a for a in indexed_apps.values() if a["name"].lower() == query_lower]
            if len(exact_matches) == 1:
                target = exact_matches[0]
            else:
                # 3. Try substring match
                matches = [a for a in indexed_apps.values() if query_lower in a["name"].lower()]
                if len(matches) == 1:
                    target = matches[0]
                elif len(matches) > 1:
                    # Ambiguity: print matching options with their numbers
                    if args.json:
                        print(json.dumps({
                            "status": "error",
                            "error": f"Multiple matches found for '{query}'",
                            "matches": matches
                        }, indent=2))
                    else:
                        print(f"Multiple matches found for '{query}':", file=sys.stderr)
                        for m in matches:
                            print(f"  [{m['index']}] {m['name']} ({m['category']})", file=sys.stderr)
                        print("\nPlease launch by entering the specific number. E.g., app_launcher.py --open <number>", file=sys.stderr)
                    sys.exit(1)

        if not target:
            print(json.dumps({
                "status": "error",
                "error": f"No application found matching '{query}'"
            }, indent=2))
            sys.exit(1)

        print(f"Launching [{target['index']}] '{target['name']}' via: {target['exec']} ...", file=sys.stderr)
        ok, err = launch_app_by_style(target)
        if ok:
            print(json.dumps({
                "status": "success",
                "launched": target["name"],
                "index": target["index"],
                "exec": target["exec"]
            }, indent=2))
            sys.exit(0)
        else:
            print(json.dumps({
                "status": "error",
                "error": f"Failed to launch '{target['name']}': {err}"
            }, indent=2))
            sys.exit(1)


if __name__ == "__main__":
    main()
