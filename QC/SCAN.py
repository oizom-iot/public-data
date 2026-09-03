import subprocess

from drivers.gpio.gpio import select_I2C
from utils.oizom_logger import OizomLogger

# -----------------------------------------------------------------------------
# Configure logging
# -----------------------------------------------------------------------------
context_logger = OizomLogger(__name__)

old_sensorList: dict[tuple[int, int], str] = {
    (0x31, 0): "CO2 Gas Sensor (ELT CO2)",
    (0x38, 0): "Sensor Box Temperature/Humidity Sensor (AHT20)",
    (0x61, 0): "4-20mA Current Loop Output (MCP4725 DAC)",
    (0x62, 0): "CO2 Gas Sensor (Sensirion SCD40)",
    (0x76, 0): "Sensor Box Temperature/Humidity/Pressure Sensor (BME280)",
    (0x77, 0): "Sensor Box Temperature/Humidity/Pressure Sensor (BME280)",
    (0x29, 1): "Ambient Light/Lux Sensor (TSL2591)",
    (0x60, 1): "UV/Ambient Light Sensor (SI1147)",
    (0x53, 1): "UV/Ambient Light Sensor (LTR390)",
    (0x41, 2): "ATHP Module Selector",
    (0x44, 2): "Temperature/Humidity Sensor (ATH/SHT)",
    (0x60, 2): "Barometric Pressure Sensor (ATH)",
    (0x5D, 2): "Barometric Pressure Sensor (LPS25HB)",
    (0x36, 3): "Battery Fuel Gauge",
}

new_sensorList: dict[tuple[int, int], str] = {
    (0x70, 0): "I2C Multiplexer (PCA9547)",
    (0x70, 1): "I2C Multiplexer (PCA9547)",
    (0x70, 2): "I2C Multiplexer (PCA9547)",
    (0x61, 2): "4-20mA Current Loop Output (MCP4725 DAC)",
    (0x70, 3): "I2C Multiplexer (PCA9547)",
    (0x41, 3): "ATHP Module Selector",
    (0x44, 3): "Temperature/Humidity Sensor (ATH/SHT)",
    (0x5D, 3): "Barometric Pressure Sensor (LPS25HB)",
    (0x60, 3): "Barometric Pressure Sensor (ATH)",
    (0x70, 4): "I2C Multiplexer (PCA9547)",
    (0x38, 4): "Sensor Box Temperature/Humidity Sensor (AHT20)",
    (0x70, 5): "I2C Multiplexer (PCA9547)",
    (0x31, 5): "CO2 Gas Sensor (ELT CO2)",
    (0x62, 5): "CO2 Gas Sensor (Sensirion SCD40)",
    (0x76, 5): "Sensor Box Temperature/Humidity/Pressure Sensor (BME280)",
    (0x77, 5): "Sensor Box Temperature/Humidity/Pressure Sensor (BME280)",
    (0x70, 6): "I2C Multiplexer (PCA9547)",
    (0x29, 6): "Ambient Light/Lux Sensor (TSL2591)",
    (0x53, 6): "UV/Ambient Light Sensor (LTR390)",
    (0x60, 6): "UV/Ambient Light Sensor (SI1147)",
    (0x70, 7): "I2C Multiplexer (PCA9547)",
    (0x36, 7): "Battery Fuel Gauge",
}


def detect_i2c_addresses(bus_number=0, timeout=3):
    """Run ``i2cdetect`` on one bus and return the list of responding addresses.

    The scan is bounded by ``timeout`` seconds. When the I2C line is hung (a
    stuck sensor holding SDA/SCL), ``i2cdetect`` blocks probing 0x03..0x77;
    the timeout kills the child process and returns ``None`` so the caller can
    skip that channel instead of stalling the whole sensor loop.

    Args:
        bus_number: I2C bus index passed to ``i2cdetect -y``.
        timeout: Max seconds to wait for the scan before aborting it.

    Returns:
        list[int] of detected addresses, ``[]`` if the bus is healthy but
        empty, or ``None`` if the scan timed out / errored.
    """
    try:
        output = subprocess.check_output(
            ["i2cdetect", "-y", str(bus_number)],
            universal_newlines=True,
            timeout=timeout,
        )
        addresses = []
        lines = output.split("\n")[1:]  # Skip the header row
        for line in lines:
            parts = line.split()
            if parts:
                for part in parts[1:]:  # Skip the first column which is the row header
                    if part not in (
                        "--",
                        "UU",
                    ):  # UU = kernel driver owns the address; no hex value to parse
                        addresses.append(int(part, 16))
        return addresses
    except subprocess.TimeoutExpired:
        context_logger.error_with_context(
            "SCAN",
            f"i2cdetect timed out after {timeout}s on bus {bus_number} - line may be hung, aborting this channel",
        )
        return None
    except subprocess.CalledProcessError as e:
        context_logger.error_with_context(
            "SCAN", f"An error occurred while detecting I2C addresses: {e}"
        )
        return None


def scan_i2c_mux() -> None:
    channel_count = 4
    # if is_pca9547_available():
    #     channel_count = 8

    sensorList = new_sensorList if channel_count == 8 else old_sensorList
    table_data = []
    for channel in range(channel_count):
        # NOTE: 📝 Here intentionally passing the same channel for both part number and channel
        select_I2C(channel, channel)
        addresses = detect_i2c_addresses()
        if addresses is None:  # scan timed out / errored on this channel
            continue
        for address in addresses:
            if (address, channel) in sensorList:
                sensor_name = sensorList[(address, channel)]
                table_data.append([channel, address, sensor_name, "Detected"])
            else:
                for (ad, ch), name in sensorList.items():
                    if ad == address:
                        sensor_name = name
                        table_data.append(
                            [
                                channel,
                                address,
                                sensor_name,
                                f"Possible channel change from {ch} to {channel}",
                            ]
                        )
                        break
                else:
                    table_data.append(
                        [channel, address, "Unknown", "Detected (not in sensor list)"]
                    )

    print("=" * 118)
    print(f"{'Channel':^8} | {'Address':^8} | {'Sensor':^58} | {'Status'}")
    print("-" * 118)

    # Print table rows
    current_channel = table_data[0][0] if table_data else 0
    for row in table_data:
        channel, addr, sensor, status = row
        if current_channel != channel:
            print("-" * 118)
            current_channel = channel
        print(f"{channel:^8} | {hex(addr):^8} | {sensor:^58} | {status}")

    print("=" * 118)


if __name__ == "__main__":
    scan_i2c_mux()
