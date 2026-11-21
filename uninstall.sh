#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    echo "Run bash uninstall.sh as your regular desktop user (without sudo)." >&2
    exit 1
fi

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
        if "iu4-millikan.desktop" in favorites:
            favorites = [app for app in favorites if app != "iu4-millikan.desktop"]
            subprocess.run(
                ["gsettings", "set", "org.gnome.shell", "favorite-apps", repr(favorites)],
                check=True, stderr=subprocess.DEVNULL,
            )
    except (OSError, SyntaxError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Could not unpin from GNOME Dock: {exc}")
PYTHON
fi
sudo rm -rf -- /opt/iu4-millikan
sudo rm -f -- /usr/share/applications/iu4-millikan.desktop /usr/local/bin/iu4-millikan
sudo update-desktop-database
echo "IU4 Millikan uninstalled. Saved experimental data was not removed."
