#!/bin/bash
#
# run this script with `curl -fsSL https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/QC/scan-update.sh | bash`

BASE_URL="https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/QC"
CONTAINER="hardware"

# ─── File definitions: filename|container_path ───
FILES=(
    "SCAN.py|/usr/src/app/QC"
    "gpio.py|/usr/src/app/drivers/gpio"
)

ANY_UPDATED=0
HAS_ERROR=0

cleanup_downloads() {
    for E in "${FILES[@]}"; do
        rm -f "/tmp/new_$(echo "$E" | cut -d'|' -f1)"
    done
}

# ─── STEP 1: Download every file before touching the container ───
# SCAN.py calls into gpio.py, so copying one in without the other would leave
# the container with a mismatched pair. Fetch both up front instead.
echo "Downloading files..."
for ENTRY in "${FILES[@]}"; do
    FILE=$(echo "$ENTRY" | cut -d'|' -f1)

    if ! curl -fsSL "$BASE_URL/$FILE" -o "/tmp/new_${FILE}"; then
        echo "Error: Failed to download '$FILE' from $BASE_URL/$FILE. Aborting — no files were changed."
        cleanup_downloads
        exit 1
    fi
    echo "  ✓ $FILE downloaded"
done
echo ""

# ─── STEP 2: Update each file in Docker ───
for ENTRY in "${FILES[@]}"; do
    FILE=$(echo "$ENTRY" | cut -d'|' -f1)
    CONTAINER_PATH=$(echo "$ENTRY" | cut -d'|' -f2)

    TEMP_DOWNLOAD="/tmp/new_${FILE}"
    TEMP_CURRENT="/tmp/current_${FILE}"

    echo "----------------------------------------"
    echo "Processing: $FILE  →  $CONTAINER_PATH"

    # Extract the current file from the container. A failure here means the
    # file is not installed yet, so fall through to the copy instead of
    # treating it as an error.
    if docker cp "${CONTAINER}:${CONTAINER_PATH}/${FILE}" "$TEMP_CURRENT" 2>/dev/null; then
        if cmp -s "$TEMP_DOWNLOAD" "$TEMP_CURRENT"; then
            echo "$FILE is already up to date"
            rm -f "$TEMP_DOWNLOAD" "$TEMP_CURRENT"
            continue
        fi
    else
        echo "$FILE not found in container — installing it for the first time"
    fi

    # Copy the file into the Docker container
    if ! docker cp "$TEMP_DOWNLOAD" "${CONTAINER}:${CONTAINER_PATH}/${FILE}"; then
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
