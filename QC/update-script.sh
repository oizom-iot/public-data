#!/bin/bash
#
# run this script with `curl -fsSL https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/NetworkManager/update-script.sh | bash`

FILE_URL="https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/QC/QC.py"
TEMP_DOWNLOAD="/tmp/QC_QC.py"

curl -fsSL "$FILE_URL" -o "$TEMP_DOWNLOAD"

if [ $? -ne 0 ]; then
    echo "Error: Failed to download file"
    exit 1
fi

docker cp "hardware:/usr/src/app/QC/QC.py" /tmp/current_QC.py

if [ $? -ne 0 ]; then
    echo "Error: Failed to extract file from Docker container"
    rm -f "$TEMP_DOWNLOAD"
    exit 1
fi

if cmp -s "$TEMP_DOWNLOAD" /tmp/current_QC.py; then
    echo "File already updated"
    rm -f "$TEMP_DOWNLOAD" /tmp/current_QC.py
    exit 0
fi

docker cp "$TEMP_DOWNLOAD" "hardware:/usr/src/app/QC/QC.py"

if [ $? -ne 0 ]; then
    echo "Error: Failed to update file in Docker container"
    rm -f "$TEMP_DOWNLOAD" /tmp/current_QC.py
    exit 1
fi

rm -f "$TEMP_DOWNLOAD" /tmp/current_QC.py

echo "QC.py updated. Please restart the device"
exit 0
