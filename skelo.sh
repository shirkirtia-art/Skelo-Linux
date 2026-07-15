#!/usr/bin/env bash
#
# Skelo — single entry point for the whole toolkit.
#
# Why this exists: an agent driving the toolkit by hand had to remember
# which of 7 different python scripts to call, with 7 different flag sets,
# for what is really one coherent workflow (see/open -> inspect/learn ->
# act). That's how steps get skipped under time pressure. skelo.sh is the
# one thing an agent needs to know: every capability in the toolkit is
# reachable as `skelo.sh <verb> [args...]`, and it forwards straight to
# the right python script with the right flags, from any working
# directory.
#
# This is a thin router, not a reimplementation — every subcommand below
# is a 1:1 pass-through to the existing scripts. Nothing here changes
# their behavior; it just gives them one shared front door.
#
# Usage:
#   skelo.sh windows
#   skelo.sh open "OBS Studio"
#   skelo.sh learn --app "OBS Studio"
#   skelo.sh click --app "Spotify" --label "Play"
#   skelo.sh minimize --app "Chrome"
#   skelo.sh doctor
#
# Run `skelo.sh help` for the full command reference.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${SKELO_PYTHON:-python3}"

run_py() {
    local script="$1"; shift
    exec "$PY" "$SCRIPT_DIR/$script" "$@"
}

usage() {
cat <<'EOF'
Skelo — one command for the whole desktop-automation toolkit.

USAGE
  skelo.sh <command> [args...]

SEEING WHAT'S ON SCREEN
  windows                                List every open window          -> list_windows.py
  apps                                   List installed applications     -> app_launcher.py --list
  open <name-or-index>                   Launch an installed app         -> app_launcher.py --open
  inspect --app <name> [--title <t>]     Dump a window's UI elements     -> inspect_window.py

LEARNING AN APP (do this before repeatedly automating something new)
  learn --app <name> [--title <t>]       check->launch->map in one call  -> skelo_learn.py  [RECOMMENDED]
  map --app <name> [--title <t>]         Map an already-open app         -> map_app.py
  resolve --skill <f> --label <l> [--click]   Replay a learned skill     -> skelo_resolve.py

ACTING (all forward to skelo_action.py)
  click --app <name> --label <l>         Click an element by live lookup
  click --x <x> --y <y>                  Click raw coordinates
  type --text "..." [--app .. --label ..]  Type text (optionally click first)
  key --key "ctrl+shift+t"               Press a key or hotkey combo (join with '+')
  scroll --amount <n>                    Scroll (+ up / - down)
  drag --x .. --y .. --x2 .. --y2 ..     Drag from one point to another
  minimize   --app <name> | --title <t>  Get a window out of the way
  maximize   --app <name> | --title <t>
  unmaximize --app <name> | --title <t>
  raise      --app <name> | --title <t>  Bring a window to the front
  close      --app <name> | --title <t>  Close a window
  alt-tab [--times N] [--reverse]        Cycle windows
  sequence '<json>'                      Run a mixed action sequence
  sequence --file <path.json>            Same, from a file
  run <raw args>                         Escape hatch: passed straight to skelo_action.py

DIAGNOSTICS
  doctor                                 Check AT-SPI/wmctrl/xdotool/pyautogui/display health
  help                                   This message

Any flags not listed above are forwarded as-is to the underlying script,
so e.g. `skelo.sh inspect --app Spotify --interactive-only` works exactly
like calling inspect_window.py directly.
EOF
}

doctor() {
    echo "== Skelo diagnostics =="

    if command -v wmctrl >/dev/null 2>&1; then
        echo "[ok]      wmctrl"
    else
        echo "[MISSING] wmctrl        -> sudo apt install wmctrl"
    fi

    if command -v xdotool >/dev/null 2>&1; then
        echo "[ok]      xdotool"
    else
        echo "[warn]    xdotool not found -> needed for 'minimize'; sudo apt install xdotool"
    fi

    if command -v xwininfo >/dev/null 2>&1; then
        echo "[ok]      xwininfo"
    else
        echo "[MISSING] xwininfo      -> sudo apt install x11-utils"
    fi

    if "$PY" -c "import gi; gi.require_version('Atspi','2.0'); from gi.repository import Atspi" >/dev/null 2>&1; then
        echo "[ok]      python3-gi / AT-SPI bindings"
    else
        echo "[MISSING] AT-SPI bindings -> sudo apt install python3-gi gir1.2-atspi-2.0 at-spi2-core"
    fi

    if "$PY" -c "import pyautogui" >/dev/null 2>&1; then
        echo "[ok]      pyautogui"
    else
        echo "[MISSING] pyautogui     -> pip install pyautogui --break-system-packages"
    fi

    if command -v gsettings >/dev/null 2>&1; then
        a11y="$(gsettings get org.gnome.desktop.interface toolkit-accessibility 2>/dev/null || echo unknown)"
        echo "[info]    toolkit-accessibility: $a11y"
        if [ "$a11y" != "true" ]; then
            echo "          -> gsettings set org.gnome.desktop.interface toolkit-accessibility true"
            echo "             (then log out/in so already-running apps pick it up)"
        fi
    fi

    if [ -n "${DISPLAY:-}" ]; then
        echo "[ok]      DISPLAY=$DISPLAY"
    else
        echo "[warn]    DISPLAY is not set — most scripts need an X11 session"
    fi

    if [ "$(id -u)" = "0" ]; then
        echo "[info]    running as root — Skelo will auto re-exec as the desktop user"
        echo "          (override with SKELO_DESKTOP_USER=<name> if it picks the wrong one)"
    fi
}

[ $# -eq 0 ] && { usage; exit 1; }
cmd="$1"; shift || true

case "$cmd" in
    windows)     run_py list_windows.py "$@" ;;
    apps)        run_py app_launcher.py --list "$@" ;;
    open)        run_py app_launcher.py --open "$@" ;;
    inspect)     run_py inspect_window.py "$@" ;;

    learn)       run_py skelo_learn.py "$@" ;;
    map)         run_py map_app.py "$@" ;;
    resolve)     run_py skelo_resolve.py "$@" ;;

    click)       run_py skelo_action.py --action click "$@" ;;
    type)        run_py skelo_action.py --action type "$@" ;;
    key)         run_py skelo_action.py --action keypress "$@" ;;
    scroll)      run_py skelo_action.py --action scroll "$@" ;;
    drag)        run_py skelo_action.py --action drag "$@" ;;

    minimize)    run_py skelo_action.py --action minimize "$@" ;;
    maximize)    run_py skelo_action.py --action maximize "$@" ;;
    unmaximize)  run_py skelo_action.py --action unmaximize "$@" ;;
    raise)       run_py skelo_action.py --action raise_window "$@" ;;
    close)       run_py skelo_action.py --action close_window "$@" ;;
    alt-tab)     run_py skelo_action.py --action alt_tab "$@" ;;

    sequence)
        if [ "${1:-}" = "--file" ]; then
            shift
            run_py skelo_action.py --file "$@"
        else
            run_py skelo_action.py --sequence "$@"
        fi
        ;;
    run)         run_py skelo_action.py "$@" ;;

    doctor)      doctor ;;
    help|-h|--help) usage ;;
    *)
        echo "Unknown command: '$cmd'" >&2
        echo >&2
        usage >&2
        exit 1
        ;;
esac
