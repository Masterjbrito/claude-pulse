#!/bin/bash
# ============================================
#  Claude Pulse - macOS installer
#  Installs deps + autostart via LaunchAgent
# ============================================
set -e
echo
echo " Claude Pulse - instalação / install"
echo

if ! command -v python3 >/dev/null; then
  echo "[ERRO] Python 3 não encontrado. / Python 3 not found."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "A instalar dependências / installing dependencies..."
python3 -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt"

PLIST="$HOME/Library/LaunchAgents/com.claudepulse.widget.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.claudepulse.widget</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v python3)</string>
    <string>${SCRIPT_DIR}/claude_pulse.py</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo
echo " Instalado! Arranca automaticamente com o macOS."
echo " Installed! Starts automatically with macOS."
