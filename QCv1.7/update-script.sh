#!/bin/bash
#
# run this script with `curl -fsSL https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/QCv1.7/update-script.sh | bash`

BASE_URL="https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/QCv1.7"
CONTAINER="hardware"
FILES=("actions.py" "checks.py" "context.py" "session.py")
CONTAINER_PATH="/usr/src/app/QC"

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

# ─── STEP 1: Download all new files ───
# All downloads finish before Docker is touched, so a failed download changes nothing.
for FILE in "${FILES[@]}"; do
    echo "Downloading $FILE..."
    if ! curl -fsSL "$BASE_URL/$FILE" -o "$TEMP_DIR/new_${FILE}"; then
        echo "Error: Failed to download '$FILE' from $BASE_URL/$FILE. Aborting — nothing was changed."
        exit 1
    fi
    echo "  ✓ $FILE downloaded"
done
echo ""

# ─── STEP 2: Update each file in Docker ───
UPDATED=0
for FILE in "${FILES[@]}"; do
    echo "----------------------------------------"
    echo "Processing: $FILE  →  $CONTAINER_PATH"

    # Extract current file from Docker container
    if ! docker cp "${CONTAINER}:${CONTAINER_PATH}/${FILE}" "$TEMP_DIR/current_${FILE}"; then
        echo "Error: Failed to extract $FILE from Docker container"
        exit 1
    fi

    # Compare files
    if cmp -s "$TEMP_DIR/new_${FILE}" "$TEMP_DIR/current_${FILE}"; then
        echo "$FILE is already up to date"
        continue
    fi

    # Copy updated file into Docker container
    if ! docker cp "$TEMP_DIR/new_${FILE}" "${CONTAINER}:${CONTAINER_PATH}/${FILE}"; then
        echo "Error: Failed to update $FILE in Docker container"
        exit 1
    fi
    echo "$FILE updated successfully"
    UPDATED=$((UPDATED + 1))
done
echo "----------------------------------------"

if [ "$UPDATED" -gt 0 ]; then
    echo "Restarting $CONTAINER container..."
    if ! docker restart "$CONTAINER"; then
        echo "Error: Failed to restart $CONTAINER container. Restart it manually."
        exit 1
    fi
    echo "  ✓ $CONTAINER container restarted"
fi

exit 0
