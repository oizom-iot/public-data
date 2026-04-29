import math
import re
import time

import serial


class Wind:
    def __init__(self):
        self.ser = None
        self.configuration = {}
        self.address = "0"
        self.debug = True
        self.part_number = 211  # 211 for 40m/s, 213 for 60m/s

        self.wind_angle = []
        self.wind_speed = []
        self.wind_gust = []

        self.max_wind_speed = 40  # default for part=1
        self.gust_avg_period = 300

        self.EOL = b"\r\n"

    def initialize(self, serial_port, configuration):
        self.ser = serial_port
        self.configuration = configuration
        self.debug = configuration.get("debug", False)
        self.part_number = configuration.get("pn", 211)
        self.gust_avg_period = configuration.get("gust_avg_period", 300)
        self.address = str(
            configuration.get("sensor_address", 0)
        )  # Taking address from config
        self.max_wind_speed = 40 if self.part_number == 211 else 60
        self.retries = 3

        try:
            for attempts in range(self.retries):
                init_response = self.verify_address()
                if len(init_response) > 3:
                    print(f"[Wind] Sensor address confirmed: {self.address}")
                    break
                print(
                    f"[Wind] Sensor address mismatch: expected {self.address}, got {init_response}. Retrying...",
                )
                if attempts == self.retries - 1:
                    print(
                        f"[Wind] Failed to confirm sensor address after {self.retries} attempts.",
                    )
                    return False

            if self.part_number == 213:
                self.set_gust_avg_period(self.gust_avg_period)

            return True

        except Exception as e:
            print(f"[Wind] initialize: {e}")
            return False

    def send_command(self, command, delay=1.5):
        """Send SDI-12 command via serial bridge (robust)."""
        # Format command with proper SDI-12 termination

        command = command.encode("ascii", errors="ignore")
        payload = command + self.EOL

        if self.debug:
            print(f"[Wind] Sending: {payload}")

        try:
            if not self.ser.is_open:
                self.ser.open()

            # Clear any pending data
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

            # Send command with proper encoding
            self.ser.write(payload)
            self.ser.flush()

            # Wait for response (SDI-12 timing)
            # ATTENTION: ⚠️ This delay is crucial for SDI-12! if you reduce it, you may miss the response or garbage response you will get.
            time.sleep(delay)

            # Read response with timeout handling
            if self.ser.in_waiting > 0:
                response_bytes = self.ser.read(self.ser.in_waiting)
                response = response_bytes.decode("ascii", errors="ignore").strip()
            else:
                response = ""

            if self.debug:
                print(f"[Wind] Raw response: {response!r}")

            return response

        except serial.SerialException as e:
            print(f"[Wind] Serial communication error: {e}")
            return ""
        except Exception as e:
            print(f"[Wind] Unexpected error in send_command: {e}")
            return ""
        finally:
            # Ensure port is closed even on error
            try:
                if self.ser and self.ser.is_open:
                    self.ser.close()
            except Exception as close_error:
                print(f"[Wind] Error closing serial port: {close_error}")

    def verify_address(self):
        """Check if sensor responds to acknowledge command (?!)"""
        return self.send_command(f"{self.address}R0!")

    def set_gust_avg_period(self, interval=300) -> None:
        response = self.send_command(f"0XST0,C={interval}!")
        if self.debug:
            print(f"[Wind] Gust avg interval set to : {response} ms")

    def parse_wind_response(self, response, cmd_type):
        """CONTEXT: 🌐 part_number=211(HONGYUV WIND SENSOR), part_number=213(HONGYUV WIND SENSOR with Gust)
        part_number=212(Calypso Wind Sensor)"""
        if not response:
            return None

        response = response.strip()
        if len(response) < 2:
            return None

        address = response[0]
        body = response[1:]

        numeric_matches = list(re.finditer(r"[+-]\d+(?:\.\d+)?", body))
        values = []
        for match in numeric_matches:
            try:
                values.append(float(match.group(0)))
            except ValueError:
                values.append(None)

        if cmd_type == "R0":
            if len(values) < 2:
                return None
            if self.part_number == 212:
                return {
                    "address": address,
                    "speed": values[0],
                    "angle": values[1],
                    "gust": None,
                }
            return {
                "address": address,
                "angle": values[0],
                "speed": values[1],
                "gust": None,
            }

        if cmd_type == "R6":
            if len(values) < 7:
                return None
            return {
                "address": address,
                "speed": values[0],
                "angle": values[3],
                "gust": values[6],
            }
        return None

    def get_speed_and_angle(self):
        try:
            if not self.ser:
                print("[Wind] Serial port not initialized")
                return None

            # ---- Standard Measurement (R0) ----
            response = self.send_command(f"{self.address}R0!")
            vals = self.parse_wind_response(response, "R0")
            if vals is not None:
                angle_value = vals.get("angle")
                speed_value = vals.get("speed")

                if angle_value is not None:
                    self.wind_angle.append(angle_value)

                if speed_value is not None:
                    self.wind_speed.append(speed_value)

                return angle_value, speed_value

            if self.debug:
                print(f"[Wind] R0 payload rejected: {response!r}")
                return None
        except Exception as e:
            print(f"[Wind] get_speed_and_angle: {e}")
            return None

    def get_gust(self):
        try:
            if not self.ser:
                print("[Wind] Serial port not initialized")
                return None

            # ---- Gust Measurement (R6) only if part_number==213 ----
            response = self.send_command(f"{self.address}R6!")
            vals6 = self.parse_wind_response(response, "R6")
            if vals6 is not None:
                gust_value = vals6.get("gust")
                if gust_value is not None:
                    self.wind_gust.append(gust_value)
                return gust_value
            if self.debug:
                print(f"[Wind] R6 payload rejected: {response!r}")
                return None
        except Exception as e:
            print(f"[Wind] get_gust: {e}")
            return None

    def vector_direction(self) -> float:
        """Vector averaging for directional data (e.g., wind direction)"""
        u = self.wind_angle
        v = self.wind_speed

        sum_u = 0.0
        sum_v = 0.0

        count = 0

        for i in range(len(u)):
            # Safety check
            if i >= len(v):
                break

            rad = math.radians(u[i])
            sum_u += math.sin(rad) * v[i]
            sum_v += math.cos(rad) * v[i]
            count += 1

        if count == 0:
            return 0.0

        avg_u = sum_u / count
        avg_v = sum_v / count

        avg_angle = (math.degrees(math.atan2(avg_u, avg_v)) + 360.0) % 360.0

        if avg_angle == 0:
            avg_angle = sum(u) / len(u)

        return int(round(avg_angle, 0))

    def vector_magnitude(self) -> float:
        """Calculate the vector magnitude from a list of values."""
        v = self.wind_speed
        u = self.wind_angle

        sum_u = 0.0
        sum_v = 0.0
        count = 0

        for i in range(len(u)):
            # Safety check
            if i >= len(v):
                break

            rad = math.radians(u[i])
            sum_u += math.sin(rad) * v[i]
            sum_v += math.cos(rad) * v[i]
            count += 1

        if count == 0:
            return 0.0

        avg_u = sum_u / count
        avg_v = sum_v / count

        magnitude = math.sqrt(avg_u**2 + avg_v**2)

        return round(magnitude, 2)

    def getSensorReading(self):
        """Get wind data from sensor and append to internal lists for averaging."""
        result = {}

        try:
            readings = self.get_speed_and_angle()
            if readings is None:
                return result

            angle_value, speed_value = readings
            gust_value = None

            if self.part_number == 213:
                gust_value = self.get_gust()

            # This part returns the latest raw values, not averages.
            for parameter in self.configuration.get("parameters", []):
                pm = parameter.get("pm", 0)

                if pm == 1 and angle_value is not None:
                    result[parameter["sc"]] = angle_value
                if pm == 2 and speed_value is not None:
                    result[parameter["sc"]] = speed_value
                if pm == 3 and self.part_number == 213 and gust_value is not None:
                    result[parameter["sc"]] = gust_value

            return result
        except Exception as e:
            print(f"[Wind] getSensorReading: {e}")
            return result

    def putsensorValue(self, result=None):
        if self.debug:
            print("[Wind] Aggregating wind data...")

        if result is None:
            result = {}

        for parameter in self.configuration.get("parameters", []):
            pm = parameter.get("pm", 0)

            if pm == 1:
                angle = self.vector_direction()
                result[parameter["sc"]] = angle
            if pm == 2:
                speed = self.vector_magnitude()
                result[parameter["sc"]] = speed
            if pm == 3 and self.part_number == 213:
                gust = (
                    sum(self.wind_gust) / len(self.wind_gust) if self.wind_gust else 0
                )
                result[parameter["sc"]] = gust
        # Clear lists for the next aggregation period
        self.wind_speed.clear()
        self.wind_angle.clear()
        self.wind_gust.clear()

        return result


if __name__ == "__main__":
    import json

    wind = Wind()
    # Note: Ensure the serial port and parameters match your device.
    # For SDI-12, baud rate is 1200, 7 data bits, even parity, 1 stop bit.
    # The pyserial default is 8N1, which might not work for all SDI-12 bridges.
    try:
        ser = serial.Serial(
            port="/dev/ttyACM0",
            baudrate=115200,
            bytesize=serial.SEVENBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            timeout=2,
        )
    except serial.SerialException as e:
        print(f"[Wind] Error opening serial port: {e}")
        exit()

    wind.initialize(
        ser,
        {
            "en": 1,
            "pn": 211,
            "debug": True,
            "sensor_address": 0,
            "parameters": [
                {"sc": "wd", "pm": 1},
                {"sc": "ws", "pm": 2},
                {"sc": "wg", "pm": 3},
            ],
        },
    )

    for i in range(5):
        print(f"[Wind] Reading {i + 1}/5...")
        wind.getSensorReading()
        time.sleep(2)

    print("Wind", "\nAggregating data...")
    data = wind.putSensorValue()
    print(f"[Wind] Final aggregated data: {json.dumps(data, indent=2)}")
