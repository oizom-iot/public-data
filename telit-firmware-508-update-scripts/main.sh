#!/bin/bash

# run this script with `curl <URL> | sudo bash`

UPDATE_FIRMWARE_SCRIPT_URL="https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/telit-firmware-508-update-scripts/update-firmware.sh"

cd /home/oizom
mkdir -p telit
cd telit

if ! wget -O update-firmware.sh "$UPDATE_FIRMWARE_SCRIPT_URL"; then
    echo "Error: Failed to download script"
    exit 1
fi

chmod +x update-firmware.sh

./update-firmware.sh 2>&1 | tee logs.txt
exit_code=${PIPESTATUS[0]}

if [ $exit_code -eq 0 ]; then
    cd /home/oizom
    rm -rf telit
else
    echo "Logs saved to /home/oizom/telit/logs.txt"
fi
