#!/usr/bin/env bash
# Installs a launcher + icon for meross_bulb.py so it appears in Rofi (drun) and the Cinnamon menu.
# Run from the folder that contains meross_bulb.py:   ./install.sh      (undo: ./install.sh --remove)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
DESKTOP="$APPS/meross-bulb.desktop"
ICON="$ICONS/meross-bulb.svg"

if [[ "${1:-}" == "--remove" ]]; then
  rm -fv "$DESKTOP" "$ICON"
  rm -fv "$HOME/.cache/rofi3.druncache"
  exit 0
fi

[[ -f "$HERE/meross_bulb.py" ]] || { echo "meross_bulb.py not found next to install.sh"; exit 1; }
chmod +x "$HERE/meross_bulb.py"
mkdir -p "$APPS" "$ICONS"

cat > "$ICON" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs><radialGradient id="g" cx="50%" cy="38%" r="60%">
    <stop offset="0" stop-color="#fff6c8"/><stop offset="1" stop-color="#ffb02e"/></radialGradient></defs>
  <path d="M32 6C20 6 12 15 12 26c0 7 3 11 7 15 2 2 3 4 3 7h20c0-3 1-5 3-7 4-4 7-8 7-15C52 15 44 6 32 6z" fill="url(#g)"/>
  <rect x="23" y="50" width="18" height="5" rx="2" fill="#8a8f98"/>
  <rect x="26" y="56" width="12" height="4" rx="2" fill="#6b7078"/>
</svg>
SVG

# Exec needs absolute paths (no ~ or $HOME), and /usr/bin/python3 so the system GTK bindings are used.
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Meross Bulbs
GenericName=Smart Bulb Control
Comment=Local LAN control for Meross RGB bulbs
Exec=/usr/bin/python3 "$HERE/meross_bulb.py"
Path=$HERE
Icon=meross-bulb
Terminal=false
Categories=Utility;Settings;
Keywords=meross;bulb;light;lamp;rgb;
StartupWMClass=meross-bulb
EOF

update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
rm -f "$HOME/.cache/rofi3.druncache"   # force Rofi to rescan on next launch

echo "Installed. Try:  rofi -show drun -show-icons   and type 'meross'"
