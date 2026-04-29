import time

import serial


class Rain:
    def __init__(self):
        self.ser: serial.Serial = None
        self.configuration = {}
        self.debug = False
        self.rain_enabled = False
        self.requested_parameters = []

    def initialize(self, serial_port, configuration):
        try:
            self.ser = serial_port
            self.configuration = configuration
            self.debug = configuration.get("debug", False)

            if configuration.get("en", 0):
                self.extract_requested_parameters(configuration)
                return True
            return False

        except Exception as e:
            print(f"[Rain] initialize: {e}")
            return False

    def extract_requested_parameters(self, configuration):
        if "parameters" in configuration:
            for parameter in configuration["parameters"]:
                param = parameter.get("sc", "rain")
                self.requested_parameters.append(param)
        return self.requested_parameters

    def send_command(self, command=b"RAIN?\r\n"):
        if self.debug:
            print(f"[Rain] Sending: {command}")

        try:
            if not self.ser.is_open:
                self.ser.open()

            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

            self.ser.write(command)
            self.ser.flush()
            time.sleep(0.3)

            response = self.ser.readline().decode().strip()
            if response.startswith("RAIN:"):
                value = float(response.split(":")[1])
                print("Rain", f"Rainfall: {value} inches")
                return value
            print(f"[Rain] Unexpected rain response: {response}")
            return 0.0

        except Exception as e:
            print("Rain", f"getSensorReading: {e}")
            return 0.0

        finally:
            # Ensure port is closed even on error
            try:
                if self.ser and self.ser.is_open:
                    self.ser.close()
            except Exception as close_error:
                print(f"[Rain] Error closing serial port: {close_error}")

    def get_rain(self):
        return self.send_command()

    def getSensorReading(self):
        pass

    def putsensorValue(self, value):
        try:
            rain = self.get_rain()
            if self.requested_parameters:
                value.update({self.requested_parameters[0]: rain})
                print(f"[Rain] Updated sensor values: {value}")
            else:
                print("[Rain] No requested parameters configured; skipping update.")

            return value
        except Exception as e:
            print(f"[Rain] putsensorValue: {e}")
            return value
