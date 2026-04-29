#!/bin/bash
#
# run this script with curl -fsSL https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/NetworkManager/update-script.sh | bash

BASE_URL="https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/QC"
FILES=("QC.py" "Noise.py" "Wind.py" "Rain.py")
CONTAINER="hardware"
CONTAINER_PATH="/usr/src/app/QC"

ANY_UPDATED=0
HAS_ERROR=0

for FILE in "${FILES[@]}"; do
    echo "----------------------------------------"
    echo "Processing: $FILE"

    TEMP_DOWNLOAD="/tmp/new_${FILE}"
    TEMP_CURRENT="/tmp/current_${FILE}"

    # Download the new file
    curl -fsSL "$BASE_URL/$FILE" -o "$TEMP_DOWNLOAD"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to download $FILE"
        HAS_ERROR=1
        rm -f "$TEMP_DOWNLOAD"
        continue
    fi

    # Extract current file from Docker container
    docker cp "${CONTAINER}:${CONTAINER_PATH}/${FILE}" "$TEMP_CURRENT"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to extract $FILE from Docker container"
        HAS_ERROR=1
        rm -f "$TEMP_DOWNLOAD" "$TEMP_CURRENT"
        continue
    fi

    # Compare files
    if cmp -s "$TEMP_DOWNLOAD" "$TEMP_CURRENT"; then
        echo "$FILE is already up to date"
        rm -f "$TEMP_DOWNLOAD" "$TEMP_CURRENT"
        continue
    fi

    # Copy updated file into Docker container
    docker cp "$TEMP_DOWNLOAD" "${CONTAINER}:${CONTAINER_PATH}/${FILE}"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to update $FILE in Docker container"
        HAS_ERROR=1
        rm -f "$TEMP_DOWNLOAD" "$TEMP_CURRENT"
        continue
    fi

    rm -f "$TEMP_DOWNLOAD" "$TEMP_CURRENT"
    echo "$FILE updated successfully"
    ANY_UPDATED=1
done

echo "----------------------------------------"
if [ $ANY_UPDATED -eq 1 ]; then
    echo "One or more files were updated. Please restart the hardware container."
fi

if [ $HAS_ERROR -eq 1 ]; then
    echo "Warning: Some files encountered errors during update."
    exit 1
fi

exit 0