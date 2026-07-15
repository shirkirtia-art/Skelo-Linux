#!/usr/bin/env bash
#
# Skelo-Linux — one-line installer
#
#   curl -fsSL https://raw.githubusercontent.com/shirkirtia-art/Skelo-Linux/main/install.sh | bash
#
# What this does:
#   1. Downloads the runtime files (not README/tests/requirements — just
#      what's needed to run the toolkit) into ~/.skelo-linux
#   2. Makes skelo.sh executable
#   3. Symlinks it as `skelo` on your PATH, so any terminal can just run
#      `skelo <command>` instead of a full path to the repo
#
# Override defaults if you need to:
#   SKELO_INSTALL_DIR=/opt/skelo-linux  SKELO_BIN_DIR=/usr/local/bin  bash install.sh
#   SKELO_BRANCH=dev  bash install.sh          # install from a different branch/tag
#
set -euo pipefail

REPO="shirkirtia-art/Skelo-Linux"
BRANCH="${SKELO_BRANCH:-main}"
RAW_BASE="${SKELO_RAW_BASE:-https://raw.githubusercontent.com/${REPO}/${BRANCH}}"
INSTALL_DIR="${SKELO_INSTALL_DIR:-$HOME/.skelo-linux}"
BIN_DIR="${SKELO_BIN_DIR:-$HOME/.local/bin}"

# Exactly the runtime files — no README, tests, requirements.txt, or
# .gitignore. Those are for people cloning the repo to develop on it, not
# for someone who just wants the `skelo` command working.
FILES=(
  skelo.sh
  skelo_common.py
  skelo_learn.py
  list_windows.py
  app_launcher.py
  inspect_window.py
  map_app.py
  skelo_resolve.py
  skelo_action.py
  SKILL.md
  LICENSE
)

c_green="\033[0;32m"; c_yellow="\033[0;33m"; c_red="\033[0;31m"; c_bold="\033[1m"; c_reset="\033[0m"
info()  { printf "${c_green}==>${c_reset} %s\n" "$1"; }
warn()  { printf "${c_yellow}==>${c_reset} %s\n" "$1"; }
error() { printf "${c_red}==>${c_reset} %s\n" "$1" >&2; }

cleanup_on_fail() {
  local ec=$?
  if [ "$ec" -ne 0 ]; then
    error "Install failed (exit $ec). Partial files may be in ${INSTALL_DIR} — safe to delete and retry."
  fi
}
trap cleanup_on_fail EXIT

if [[ "$(uname -s)" != "Linux" ]]; then
  error "Skelo-Linux automates Linux desktops (X11) only. Detected: $(uname -s)"
  exit 1
fi

if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -q "$1" -O "$2"; }
else
  error "Neither curl nor wget is installed. Install one of them and re-run."
  exit 1
fi

printf "${c_bold}Skelo-Linux installer${c_reset}\n"
info "Source:      https://github.com/${REPO} (${BRANCH})"
info "Install dir: ${INSTALL_DIR}"
echo

mkdir -p "${INSTALL_DIR}/skills"

for f in "${FILES[@]}"; do
  printf "  fetching %-22s" "$f"
  if fetch "${RAW_BASE}/${f}" "${INSTALL_DIR}/${f}.part"; then
    mv "${INSTALL_DIR}/${f}.part" "${INSTALL_DIR}/${f}"
    echo "done"
  else
    rm -f "${INSTALL_DIR}/${f}.part"
    echo "FAILED"
    error "Could not fetch ${f} from ${RAW_BASE}."
    error "Check that the repo is public and branch '${BRANCH}' exists."
    exit 1
  fi
done

chmod +x "${INSTALL_DIR}/skelo.sh"

mkdir -p "${BIN_DIR}"
ln -sf "${INSTALL_DIR}/skelo.sh" "${BIN_DIR}/skelo"

echo
info "Installed. skelo.sh linked as 'skelo' at ${BIN_DIR}/skelo"

case ":$PATH:" in
  *":${BIN_DIR}:"*)
    ;;
  *)
    warn "${BIN_DIR} isn't on your PATH yet. Add this to ~/.bashrc or ~/.zshrc:"
    echo
    echo "    export PATH=\"${BIN_DIR}:\$PATH\""
    echo
    warn "Then restart your shell, or run: export PATH=\"${BIN_DIR}:\$PATH\""
    ;;
esac

echo
info "Next steps:"
echo "    skelo doctor      # check AT-SPI/wmctrl/xdotool/pyautogui are all in place"
echo "    skelo help        # full command reference"
echo
info "Full docs: ${INSTALL_DIR}/SKILL.md"

trap - EXIT
