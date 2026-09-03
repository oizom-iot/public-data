#!/bin/bash
#
# run this script with `curl -fsSL https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/QC/scan-update.sh | bash`

BASE_URL="https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/QC"
CONTAINER="hardware"
FILE="SCAN.py"
CONTAINER_PATH="/usr/src/app/QC"

TEMP_DOWNLOAD="/tmp/new_${FILE}"
TEMP_CURRENT="/tmp/current_${FILE}"

# ─── STEP 1: Download the new file ───
echo "Downloading $FILE..."
if ! curl -fsSL "$BASE_URL/$FILE" -o "$TEMP_DOWNLOAD"; then
    echo "Error: Failed to download '$FILE' from $BASE_URL/$FILE. Aborting — nothing was changed."
    rm -f "$TEMP_DOWNLOAD"
    exit 1
fi
echo "  ✓ $FILE downloaded"
echo ""

# ─── STEP 2: Update the file in Docker ───
echo "----------------------------------------"
echo "Processing: $FILE  →  $CONTAINER_PATH"

# Extract current file from Docker container. A failure here means the file is
# not installed yet, so fall through to the copy instead of aborting.
if docker cp "${CONTAINER}:${CONTAINER_PATH}/${FILE}" "$TEMP_CURRENT" 2>/dev/null; then
    if cmp -s "$TEMP_DOWNLOAD" "$TEMP_CURRENT"; then
        echo "$FILE is already up to date"
        rm -f "$TEMP_DOWNLOAD" "$TEMP_CURRENT"
        exit 0
    fi
else
    echo "$FILE not found in container — installing it for the first time"
fi

# Copy the file into the Docker container
if ! docker cp "$TEMP_DOWNLOAD" "${CONTAINER}:${CONTAINER_PATH}/${FILE}"; then
    echo "Error: Failed to update $FILE in Docker container"
    rm -f "$TEMP_DOWNLOAD" "$TEMP_CURRENT"
    exit 1
fi

rm -f "$TEMP_DOWNLOAD" "$TEMP_CURRENT"
echo "$FILE updated successfully"
echo "----------------------------------------"
echo "Please restart the hardware container."

exit 0
