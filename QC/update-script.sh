#!/bin/bash
#
# run this script with curl -fsSL https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/NetworkManager/update-script.sh | bash

BASE_URL="https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/QC"
CONTAINER="hardware"

# ─── File definitions: name|repo_subfolder|container_path ───
FILES=(
    "QC.py|QC|/usr/src/app/QC"
    "Noise.py|Noise|/usr/src/app/drivers/Noise"
    "Wind.py|Wind|/usr/src/app/drivers/Wind"
    "Rain.py|Rain|/usr/src/app/drivers/Rain"
)

ANY_UPDATED=0
HAS_ERROR=0

# ─── STEP 1: Verify all 4 files exist on GitHub before doing anything ───
echo "Verifying all files are present on GitHub..."
for ENTRY in "${FILES[@]}"; do
    FILE=$(echo "$ENTRY" | cut -d'|' -f1)
    SUBFOLDER=$(echo "$ENTRY" | cut -d'|' -f2)
    FILE_URL="https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/$SUBFOLDER/$FILE"

    if ! curl -fsSL --head "$FILE_URL" -o /dev/null 2>/dev/null; then
        echo "Error: '$FILE' not found on GitHub at $FILE_URL. Aborting — no files were changed."
        exit 1
    fi
    echo "  ✓ $FILE found"
done
echo "All files verified. Proceeding with update..."
echo ""

# ─── STEP 2: Download all 4 files first ───
echo "Downloading all files..."
for ENTRY in "${FILES[@]}"; do
    FILE=$(echo "$ENTRY" | cut -d'|' -f1)
    SUBFOLDER=$(echo "$ENTRY" | cut -d'|' -f2)
    FILE_URL="https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/$SUBFOLDER/$FILE"
    TEMP_DOWNLOAD="/tmp/new_${FILE}"

    curl -fsSL "$FILE_URL" -o "$TEMP_DOWNLOAD"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to download '$FILE'. Aborting — cleaning up."
        for E in "${FILES[@]}"; do
            F=$(echo "$E" | cut -d'|' -f1)
            rm -f "/tmp/new_${F}"
        done
        exit 1
    fi
    echo "  ✓ $FILE downloaded"
done
echo ""

# ─── STEP 3: Now update each file in Docker ───
for ENTRY in "${FILES[@]}"; do
    FILE=$(echo "$ENTRY" | cut -d'|' -f1)
    CONTAINER_PATH=$(echo "$ENTRY" | cut -d'|' -f3)

    echo "----------------------------------------"
    echo "Processing: $FILE  →  $CONTAINER_PATH"

    TEMP_DOWNLOAD="/tmp/new_${FILE}"
    TEMP_CURRENT="/tmp/current_${FILE}"

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