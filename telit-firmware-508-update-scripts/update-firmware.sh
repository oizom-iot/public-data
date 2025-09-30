#!/bin/bash

UXPF_URL="https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/telit-firmware-508-update-scripts/uxfp-compiled-on-cm4"
BINARY_URL="https://raw.githubusercontent.com/oizom-iot/public-data/refs/heads/main/telit-firmware-508-update-scripts/LE910C4-WWX_25.30.508_CUST_057_10_STR.bin"

identify_module() {
    # Get the list of USB devices
    usb_info=$(lsusb)

    # Check for Telit module
    if echo "$usb_info" | grep -iq "1bc7"; then
        echo "Telit Module Detected"
    else
        post_script_tasks
        echo "No Telit Module Found. Exiting."
        exit 0
    fi
}

module_bootmode_status() {
    local port_count=$(ls -1 /dev/ttyUSB* | wc -l)
    if [ "$port_count" = "3" ]; then
        return 0
    else
        echo "Module is already in boot mode"
        return 1
    fi
}

get_highest_gsm_port() {
    local highest=$(ls -1 /dev/ttyUSB* 2>/dev/null | grep -o '/dev/ttyUSB[0-9]\+' | sort -V | tail -n 1)
    echo "${highest:-/dev/ttyUSB-1}"
}

get_lowest_gsm_port() {
    local lowest=$(ls -1 /dev/ttyUSB* 2>/dev/null | grep -o '/dev/ttyUSB[0-9]\+' | sort -V | head -n 1)
    echo "${lowest:-/dev/ttyUSB-1}"
}

pre_script_tasks() {
    echo "Stopping gsm.service and hardware container"
    systemctl stop gsm.service
    docker stop hardware
}

post_script_tasks() {
    echo "Starting gsm.service and hardware container"
    systemctl start gsm.service
    docker start hardware
}

check_software_package_version() {
    echo "Checking Telit Software Package Version"

    temp_file=$(mktemp)
    oizom-config --gsmport=$(get_highest_gsm_port) --modemcommand="AT#SWPKGV" | tee "$temp_file"
    
    response=$(cat "$temp_file")
    rm "$temp_file"
    
    # Find the line that matches the pattern XX.XX.XXX-XXX.XXXXXX (version line)
    version_line=$(echo "$response" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+-')
    
    # Extract text between second period and first hyphen
    # eg '528' from 25.30.528-P0F.525703
    version=$(echo "$version_line" | sed -n 's/^[^.]*\.[^.]*\.\([^-]*\)-.*/\1/p')
    
    # Check if version contains exactly 3 digits
    if [[ ! "$version" =~ ^[0-9]{3}$ ]]; then
        post_script_tasks
        echo "Error: Can not find 3 digit version number. Exiting."
        exit 1
    fi
    
    echo "Found Telit Software Package Version: $version"
    
    # Check if current version matches binary version
    if [ "$version" = "508" ] || [ "$version" = "528" ]; then
        post_script_tasks
        echo "Telit version matches binary version. No need to update. Exiting."
        exit 0
    fi
}

download_files() {
    # Download UXPF executable
    if [ -f "uxfp" ]; then
        echo "UXPF file already exists, skipping download"
    else
        echo "Downloading update tool UXPF"
        if ! wget -O uxfp "$UXPF_URL"; then
            post_script_tasks
            echo "Error: Failed to download UXPF file"
            exit 1
        fi
    fi
    chmod +x uxfp
    
    # Download firmware binary
    if [ -f "firmware.bin" ]; then
        echo "Firmware binary already exists, skipping download"
    else
        echo "Downloading firmware binary"
        if ! wget -O firmware.bin "$BINARY_URL"; then
            post_script_tasks
            echo "Error: Failed to download firmware binary"
            exit 1
        fi
    fi
}

wait_for_gsm_port() {
    local timeout=30  # total timeout in seconds
    local interval=1
    local elapsed=0

    echo "Waiting for GSM module to appear via lsusb..."
    sleep 10

    while [ $elapsed -lt $timeout ]; do
        if lsusb | grep -qiE '1bc7'; then
            echo "GSM module detected via lsusb"
            return 0
        fi
        sleep $interval
        elapsed=$((elapsed + interval))
    done

    post_script_tasks
    echo "Timeout: GSM module not detected in lsusb after reboot. Exiting."
    exit 1
}

reboot_module() {
    oizom-config --gsmport=$(get_highest_gsm_port) --modemcommand="AT#REBOOT"
    wait_for_gsm_port
}

update_firmware_via_uart() {
    if module_bootmode_status; then
        echo "Running initial firmware update command..."
        
        # Run first command in background and capture its PID
        ./uxfp --file firmware.bin --no-fastboot-driver --debug --port $(get_lowest_gsm_port) --serial --speed 115200 --report &
        local first_pid=$!
        
        # Wait for the specified timeout, then kill the process
        local hang_timeout=25
        local elapsed=0
        
        while kill -0 $first_pid 2>/dev/null && [ $elapsed -lt $hang_timeout ]; do
            sleep 1
            elapsed=$((elapsed + 1))
        done
        
        # If process is still running after timeout, kill it
        if kill -0 $first_pid 2>/dev/null; then
            echo "First command timed out after ${hang_timeout} seconds, killing process..."
            kill $first_pid 2>/dev/null
            wait $first_pid 2>/dev/null
        else
            echo "First command completed normally"
        fi
        
        echo "Running recovery command..."
        ./uxfp --file firmware.bin --no-fastboot-driver --lossrecovery --debug --port $(get_highest_gsm_port) --serial --speed 115200 --report
    else
        ./uxfp --file firmware.bin --no-fastboot-driver --lossrecovery --debug --port $(get_highest_gsm_port) --serial --speed 115200 --report
    fi
    if [ $? -ne 0 ]; then
        post_script_tasks
        echo "Update failed. Please try again."
        exit 1
    fi
    echo "Waiting for 30s..."
    sleep 30
}

configure_telit() {
    wait_for_gsm_port || exit 1
    echo "Setting global firmware..."
    oizom-config --gsmport=$(get_highest_gsm_port) --modemcommand="AT#FWSWITCH=40,1,0"
    echo "Waiting for module to reboot..."
    sleep 45
    fwswitch_status=$(oizom-config --gsmport=$(get_highest_gsm_port) --modemcommand="AT#FWSWITCH?" | grep -c 'FWSWITCH: 40,0,0')
    if [ "$fwswitch_status" -eq 0 ]; then
        echo "Failed to set FWSWITCH. Exiting."
        exit 1
    fi

    echo "Setting auto firmware..."
    oizom-config --gsmport=$(get_highest_gsm_port) --modemcommand="AT#FWAUTOSIM=1"
    sleep 5
    fwautosim_status=$(oizom-config --gsmport=$(get_highest_gsm_port) --modemcommand="AT#FWSWITCH?" | grep -c 'FWSWITCH: 40,0,0')
    if [ "$fwautosim_status" -eq 0 ]; then
        echo "Failed to set FWAUTOSIM. Exiting."
        exit 1
    fi

    sleep 5
    echo "Clearing APN settings..."
    oizom-config --gsmport=$(get_highest_gsm_port) --modemcommand="AT+CGDCONT=1,\"IPV4V6\",\"\""
    sleep 5
    cgdcont_status=$(oizom-config --gsmport=$(get_highest_gsm_port) --modemcommand="AT+CGDCONT?" | grep -c 'CGDCONT: 1,"IPV4V6","","",0,0,0,0')
    if [ "$cgdcont_status" -eq 0 ]; then
        echo "Failed to clear APN settings. Exiting."
        exit 1
    fi

    echo "Setting USBCFG..."
    wait_for_gsm_port || exit 1
    oizom-config --gsmport=$(get_highest_gsm_port) --modemcommand="AT#USBCFG=1"
    wait_for_gsm_port || exit 1
    usbcfg_status=$(oizom-config --gsmport=$(get_highest_gsm_port) --modemcommand="AT#USBCFG?" | grep -c 'USBCFG: 1')
    if [ "$usbcfg_status" -eq 0 ]; then
        echo "Failed to set USBCFG. Exiting."
        exit 1
    fi

    echo "Setting ECM..."
    wait_for_gsm_port || exit 1
    oizom-config --gsmport=$(get_highest_gsm_port) --modemcommand="AT#ECM=1,0"
    sleep 5
    ecm_status=$(oizom-config --gsmport=$(get_highest_gsm_port) --modemcommand="AT#ECM?" | grep -c 'ECM: 0,1')
    if [ "$ecm_status" -eq 0 ]; then
        echo "Failed to set ECM. Exiting."
        exit 1
    fi
}

pre_script_tasks
identify_module
download_files
if module_bootmode_status; then
    check_software_package_version
    reboot_module
fi
update_firmware_via_uart
configure_telit
post_script_tasks

echo "Update successful. Please update debian package."
exit 0