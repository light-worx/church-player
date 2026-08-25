#!/usr/bin/env bash
# Installs a "Church Media Player" shortcut into your applications menu.
# Run this once, from the same folder where church_player.py lives:
#
#   chmod +x install_shortcut.sh
#   ./install_shortcut.sh

set -e

# Directory this script lives in (assumes church_player.py is right next to it)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLAYER_PATH="$SCRIPT_DIR/church_player.py"

if [ ! -f "$PLAYER_PATH" ]; then
    echo "Could not find church_player.py in $SCRIPT_DIR"
    echo "Make sure install_shortcut.sh sits in the same folder as church_player.py."
    exit 1
fi

DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$DESKTOP_DIR/church-player.desktop"

mkdir -p "$DESKTOP_DIR"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Church Media Player
Comment=Play music/video through the PA system
Exec=python3 "$PLAYER_PATH"
Icon=multimedia-player
Terminal=false
Categories=AudioVideo;Player;
EOF

chmod +x "$DESKTOP_FILE"

# Refresh the desktop database if the tool is available (not essential)
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

echo "Shortcut installed! You should now find 'Church Media Player' in your"
echo "applications menu (under Sound & Video / AudioVideo)."
echo "It may take a moment, or a logout/login, to appear."