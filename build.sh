#!/usr/bin/env sh
# Build a standalone binary on Linux (or macOS). Usage: ./build.sh
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --name smartmonitor --windowed \
  --collect-binaries libusb_package --add-data "themes:themes" \
  --add-data "translations:translations" smartmonitor/__main__.py
echo "Built dist/smartmonitor/smartmonitor"
echo "Install the udev rule for non-root USB access:"
echo "  sudo cp packaging/99-smartmonitor.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger"
