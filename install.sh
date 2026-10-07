#!/usr/bin/env bash
# Usage: ./install.sh [--launchd]
#   Installs adw into ADW_HOME (default $HOME/.claude/adw), puts its commands on PATH via
#   ~/.local/bin, and links its skills into ~/.claude/skills. Safe to re-run.
#   --launchd  also render and load a launchd agent that keeps the ADW Board running (macOS).
#   Env: ADW_HOME, ADW_BIN_DIR (default ~/.local/bin), ADW_LAUNCHD_LABEL (default com.example.adw-board).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ADW_HOME="${ADW_HOME:-$HOME/.claude/adw}"
BIN_DIR="${ADW_BIN_DIR:-$HOME/.local/bin}"
SKILLS_DIR="$HOME/.claude/skills"
LABEL="${ADW_LAUNCHD_LABEL:-com.example.adw-board}"

want_launchd=0
for arg in "$@"; do
  case "$arg" in
    --launchd) want_launchd=1 ;;
    -h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

note() { printf '  %s\n' "$*"; }

link() {
  local src="$1" dest="$2"
  if [ -L "$dest" ]; then
    if [ "$(readlink "$dest")" = "$src" ]; then
      return 0
    fi
    ln -sfn "$src" "$dest"
    note "relinked $dest -> $src"
  elif [ -e "$dest" ]; then
    note "SKIPPED $dest: exists and is not a symlink (move it aside to let adw manage it)"
  else
    ln -s "$src" "$dest"
    note "linked $dest -> $src"
  fi
}

echo "adw: installing from $REPO"
mkdir -p "$ADW_HOME" "$ADW_HOME/epics" "$BIN_DIR" "$SKILLS_DIR"

echo "commands -> $BIN_DIR"
for f in "$REPO"/bin/*; do
  [ -f "$f" ] || continue
  chmod +x "$f"
  link "$f" "$BIN_DIR/$(basename "$f")"
done
for pair in "adw-board:board/board.py" "adw-ask:board/asks.py" "adw-status:board/status.py"; do
  name="${pair%%:*}"
  src="$REPO/${pair#*:}"
  if [ -f "$src" ]; then
    chmod +x "$src"
    link "$src" "$BIN_DIR/$name"
  else
    note "missing $src; $name not installed"
  fi
done

echo "skills -> $SKILLS_DIR"
for d in "$REPO"/skills/*/; do
  [ -d "$d" ] || continue
  d="${d%/}"
  link "$d" "$SKILLS_DIR/$(basename "$d")"
done

echo "engine -> $ADW_HOME"
link "$REPO/review" "$ADW_HOME/review"
link "$REPO/board" "$ADW_HOME/board"
link "$REPO/docs" "$ADW_HOME/docs"

echo "config -> $ADW_HOME"
for f in "$REPO"/config/*.example.json; do
  [ -f "$f" ] || continue
  base="$(basename "$f")"
  dest="$ADW_HOME/${base/.example/}"
  if [ -e "$dest" ]; then
    note "kept existing $dest"
  else
    cp "$f" "$dest"
    note "created $dest (edit it)"
  fi
done

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "note: $BIN_DIR is not on your PATH; add it in your shell profile." ;;
esac

if [ "$want_launchd" -eq 1 ]; then
  if [ "$(uname -s)" != "Darwin" ]; then
    echo "--launchd is macOS only; start the board yourself with: adw-board --serve 4518" >&2
    exit 1
  fi
  template="$REPO/launchd/adw-board.plist.template"
  plist="$HOME/Library/LaunchAgents/$LABEL.plist"
  python_bin="$(command -v python3)"
  mkdir -p "$HOME/Library/LaunchAgents"
  sed -e "s#com\.example\.adw-board#$LABEL#g" \
      -e "s#__PYTHON__#$python_bin#g" \
      -e "s#__ADW_HOME__#$ADW_HOME#g" \
      -e "s#__HOME__#$HOME#g" \
      "$template" > "$plist"
  plutil -lint "$plist" >/dev/null
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "launchd: loaded $LABEL ($plist); board on http://127.0.0.1:4518/"
fi

echo "done. Next: edit $ADW_HOME/manifest.json, then run: adw-board --serve 4518"
