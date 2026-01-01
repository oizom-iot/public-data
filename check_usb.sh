#!/bin/bash

LOGFILE="$(dirname "$0")/check_usb.log"

log_message() {
    echo "$(date): $1" >> $LOGFILE
}

found_arduino="0"

check_usb() {

    #log_message "Starting USB check"

    for sysdevpath in $(find /sys/bus/usb/devices/usb*/ -name dev); do
        syspath="${sysdevpath%/dev}"
        devname="$(udevadm info -q name -p $syspath)"
        [[ "$devname" == "bus/"* ]] && continue
        eval "$(udevadm info -q property --export -p $syspath)"
        [[ -z "$ID_SERIAL" ]] && continue

        if [[ "$ID_SERIAL" == *"Arduino_LLC_Arduino_Zero"* ]] || [[ "$devname" == "ttyACM0" ]]  || [[ "$devname" == "ttyACM1" ]]; then
            found_arduino="1"
            #log_message "Found: /dev/$devname - $ID_SERIAL"
            break
        fi
    done

    if [[ "$found_arduino" == "0" ]]; then
        log_message "Arduino_LLC_Arduino_Zero or ttyACM0 not found."
    fi

}

check_usb

if [[ "$found_arduino" == "0" ]]; then
    log_message "Reboot flag set. Rebooting the system."
    log_message "Appending last reboot logs"
    sleep 5
    dmesg -T | tail -50 >> $LOGFILE
    docker logs hardware --tail 1000 --timestamps >> $LOGFILE
    sudo reboot
fi
