#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    echo "Run bash install.sh as your regular desktop user (without sudo)." >&2
    exit 1
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for file in iu4millikan.py run.sh icon.png Screenshot-IU4.png README.md; do
    [[ -f "$SOURCE_DIR/$file" ]] || { echo "Missing $file" >&2; exit 1; }
done

sudo apt-get update
sudo apt-get install -y python3-venv python3-pip v4l-utils libgl1 libglib2.0-0 libxcb-cursor0 desktop-file-utils

sudo rm -rf -- /opt/iu4-millikan
sudo install -d -m 0755 /opt/iu4-millikan
sudo install -m 0644 "$SOURCE_DIR/iu4millikan.py" "$SOURCE_DIR/README.md" "$SOURCE_DIR/icon.png" "$SOURCE_DIR/Screenshot-IU4.png" /opt/iu4-millikan/
sudo install -m 0755 "$SOURCE_DIR/run.sh" /opt/iu4-millikan/run.sh

sudo python3 -m venv /opt/iu4-millikan/venv
sudo /opt/iu4-millikan/venv/bin/python -m pip install --upgrade pip
sudo /opt/iu4-millikan/venv/bin/python -m pip install opencv-python PyQt6 numpy
sudo chmod -R a+rX /opt/iu4-millikan

sudo tee /usr/share/applications/iu4-millikan.desktop >/dev/null <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=IU4 Millikan Experiment
Comment=Oil Drop Experiment Application
Exec=/opt/iu4-millikan/run.sh
Icon=/opt/iu4-millikan/icon.png
Terminal=false
Categories=Education;Science;
DESKTOP
sudo chmod 0644 /usr/share/applications/iu4-millikan.desktop
sudo ln -sfn /opt/iu4-millikan/run.sh /usr/local/bin/iu4-millikan
sudo update-desktop-database

if command -v gsettings >/dev/null 2>&1; then
python3 - <<'PYTHON'
import ast
import os
import subprocess
if "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    try:
        raw = subprocess.check_output(
            ["gsettings", "get", "org.gnome.shell", "favorite-apps"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        favorites = ast.literal_eval(raw.removeprefix("@as "))
        if "iu4-millikan.desktop" not in favorites:
            favorites.append("iu4-millikan.desktop")
            subprocess.run(
                ["gsettings", "set", "org.gnome.shell", "favorite-apps", repr(favorites)],
                check=True, stderr=subprocess.DEVNULL,
            )
    except (OSError, SyntaxError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Could not pin to GNOME Dock: {exc}")
PYTHON
fi
echo "IU4 Millikan installed. Launch from Applications/Dock or run: iu4-millikan"
