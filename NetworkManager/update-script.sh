#!/bin/bash
#
# run this script with `curl -fsSL https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/NetworkManager/update-script.sh | bash`

FILE_URL="https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/NetworkManager/main_production-1.6.py"
TEMP_DOWNLOAD="/tmp/NetworkManager_main_production-1.6.py"

if ! lsusb | grep -q "0bda:8179"; then
    echo "Error: VID:PID ${REQUIRED_USB_DEVICE} not found"
    echo "Please connect USB WiFi dongle and try again"
    exit 1
fi

curl -fsSL "$FILE_URL" -o "$TEMP_DOWNLOAD"

if [ $? -ne 0 ]; then
    echo "Error: Failed to download file"
    exit 1
fi

docker cp "networkmanager:/usr/src/app/main.py" /tmp/current_file.py

if [ $? -ne 0 ]; then
    echo "Error: Failed to extract file from Docker container"
    rm -f "$TEMP_DOWNLOAD"
    exit 1
fi

if cmp -s "$TEMP_DOWNLOAD" /tmp/current_file.py; then
    echo "File already updated"
    rm -f "$TEMP_DOWNLOAD" /tmp/current_file.py
    exit 0
fi

docker cp "$TEMP_DOWNLOAD" "networkmanager:/usr/src/app/main.py"

if [ $? -ne 0 ]; then
    echo "Error: Failed to update file in Docker container"
    rm -f "$TEMP_DOWNLOAD" /tmp/current_file.py
    exit 1
fi

rm -f "$TEMP_DOWNLOAD" /tmp/current_file.py

echo "main.py updated. Please restart the device"
exit 0
