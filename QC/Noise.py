import math
import time


class Noise:
    def __init__(self):
        self.FRAME_LEN = 8
        self.TIMEOUT = 1.6
        self.ser = None
        self.configuration = {}
        self.LAeq = []
        self.LZeq = []
        self.debug = False
        self.part_number = 41
        self.old_laeq = 0.0
        self.old_max_laeq = 0.0
        self.old_min_laeq = 0.0

        self.EOL = b"\r\n"

    def initialize(self, serial_port, configuration):
        try:
            self.ser = serial_port
            self.configuration = configuration
            self.debug = configuration.get("debug", False)
            self.part_number = configuration.get("pn", 41)

            if configuration.get("en", 0):
                return True  # TODO: 📋 Write perfect init code
            return False

        except Exception as e:
            print(f"[Noise] initialize: {e}")
            return False

    def send_command(self, command=b"NOISE?\r\n", delay=0.5):
        """Send command and read 8-byte frame response."""
        if self.debug:
            print(f"[Noise] Sending: {command}")

        try:
            if not self.ser.is_open:
                self.ser.open()

            # Clear any pending data
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

            # Send command
            self.ser.write(command)
            self.ser.flush()

            # Give SAMD time to process and respond
            time.sleep(delay)

            # Read 8-byte frame response
            if self.ser.in_waiting > 0:
                buf = bytearray()
                start = time.time()

                while len(buf) < self.FRAME_LEN:
                    chunk = self.ser.read(self.FRAME_LEN - len(buf))
                    if chunk:
                        buf.extend(chunk)
                        if self.debug:
                            print(f"[Noise] Read {len(chunk)} bytes: {chunk.hex()}")
                    if time.time() - start > self.TIMEOUT:
                        if self.debug:
                            print(
                                f"[Noise] Timeout! Only got {len(buf)} bytes: {buf.hex() if buf else 'empty'}"
                            )
                        return None

                if self.debug:
                    print(f"[Noise] Complete frame ({len(buf)} bytes): {buf.hex()}")
                    print(f"[Noise] Frame breakdown: {[hex(b) for b in buf]}")

                return bytes(buf)
            if self.debug:
                print("[Noise] No data in buffer")
            return None

        except Exception as e:
            print(f"[Noise] send_command error: {e}")
            return None
        finally:
            # Ensure port is closed even on error
            try:
                if self.ser and self.ser.is_open:
                    self.ser.close()
            except Exception as close_error:
                print(f"[Noise] Error closing serial port: {close_error}")

    def parse_noise_response(self, frame):
        """Parse 8-byte frame to extract LAeq and LZeq values."""
        laeq = None
        lzeq = None

        if not frame:
            return None

        if len(frame) != self.FRAME_LEN:
            if self.debug:
                print(
                    f"[Noise] Invalid frame length: {len(frame)} (expected {self.FRAME_LEN})"
                )
            return None

        if self.debug:
            print(
                f"[Noise] Checking frame header: [0]={hex(frame[0])}, [1]={hex(frame[1])}, [7]={hex(frame[7])}"
            )

        # Validate frame format: 0x01 0x03 ... 0x0A
        if frame[0] != 0x01 or frame[1] != 0x03 or frame[7] != 0x0A:
            if self.debug:
                print("[Noise] Invalid frame format!")
            return None

        # Extract LAeq and LZeq from bytes 3-6
        laeq = ((frame[3] << 8) | frame[4]) / 10.0
        lzeq = ((frame[5] << 8) | frame[6]) / 10.0

        if self.debug:
            print(f"[Noise] Parsed -> LAeq: {laeq} dB, LZeq: {lzeq} dB")

        return {"LAeq": laeq, "LZeq": lzeq}

    def get_noise(self):
        """Get noise data from sensor - sends command and parses response."""
        try:
            if not self.ser:
                print("[Noise] Serial port not initialized")
                return None

            frame = self.send_command()
            if frame is None:
                return None

            result = self.parse_noise_response(frame)

            if result is not None:
                laeq = result.get("LAeq")
                lzeq = result.get("LZeq")

                if laeq is not None:
                    self.LAeq.append(laeq)
                if lzeq is not None:
                    self.LZeq.append(lzeq)

            return result
        except Exception as e:
            print(f"[Noise] get_noise: {e}")
            return None

    def energy_average(self) -> float:
        """Energy averaging for dB values - acoustically correct"""
        energy_sum = 0.0
        energy_avg = 0.0
        result = 0.0

        try:
            values = self.LAeq
            # Acoustic energy averaging: 10*log10(mean(10^(dB/10)))
            max_val = max(values)  # For numerical stability
            for val in values:
                energy_sum += 10 ** ((val - max_val) / 10.0)

            energy_avg = energy_sum / len(values)

            if energy_avg <= 0:
                return 0.0

            result = max_val + 10.0 * math.log10(energy_avg)
            return round(result, 2)

        except (OverflowError, ValueError, ZeroDivisionError):
            # Fallback to arithmetic mean
            value = sum(values) / len(values)
            return round(value, 2)

    def getSensorReading(self):
        """Get noise sensor reading and append to internal lists for averaging."""
        result = {}

        try:
            readings = self.get_noise()
            if readings is None:
                return result

            laeq_value = readings.get("LAeq")
            lzeq_value = readings.get("LZeq")

            for parameter in self.configuration.get("parameters", []):
                pm = parameter.get("pm", 0)

                if pm == 1 and laeq_value is not None:
                    result[parameter["sc"]] = laeq_value
                if pm == 2 and len(self.LAeq) > 0:
                    result[parameter["sc"]] = max(self.LAeq)
                if pm == 3 and len(self.LAeq) > 0:
                    result[parameter["sc"]] = min(self.LAeq)
            return result

        except Exception as e:
            print(f"[Noise] getSensorReading: {e}")
            return result

    def putsensorValue(self, result=None):
        """Aggregate and return collected noise data."""
        if self.debug:
            print("[Noise] Aggregating Noise data...")

        if result is None:
            result = {}

        for parameter in self.configuration.get("parameters", []):
            pm = parameter.get("pm", 0)

            if len(self.LAeq) > 0:
                if pm == 1:
                    self.old_laeq = self.energy_average()
                    result[parameter["sc"]] = self.old_laeq
                if pm == 2:
                    self.old_max_laeq = max(self.LAeq)
                    result[parameter["sc"]] = self.old_max_laeq
                if pm == 3:
                    self.old_min_laeq = min(self.LAeq)
                    result[parameter["sc"]] = self.old_min_laeq
            else:
                if pm == 1:
                    result[parameter["sc"]] = self.old_laeq
                if pm == 2:
                    result[parameter["sc"]] = self.old_max_laeq
                if pm == 3:
                    result[parameter["sc"]] = self.old_min_laeq

        # Clear lists for the next aggregation period
        self.LAeq.clear()
        self.LZeq.clear()

        return result


if __name__ == "__main__":
    import serial

    print("[Noise Test] Starting noise sensor test on /dev/ttyACM0")

    try:
        # Open serial port
        ser = serial.Serial(port="/dev/ttyACM0", baudrate=115200, timeout=2)

        print(f"[Noise Test] Serial port opened: {ser.name}")

        # Initialize noise sensor
        noise = Noise()
        config = {"en": 1}

        if noise.initialize(ser, config):
            print("[Noise Test] ✅ Noise sensor initialized")
        else:
            print("[Noise Test] ❌ Failed to initialize")
            exit(1)

        # Performance tracking
        response_times = []
        byte_times = []

        # Test loop
        print("\n[Noise Test] Starting continuous readings (Ctrl+C to stop)...\n")

        iteration = 0
        while True:
            iteration += 1
            print(f"\n{'=' * 60}")
            print(f"Iteration #{iteration}")
            print(f"{'=' * 60}")

            # Clear buffers
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            # Measure command send time
            cmd_start = time.time()
            ser.write(b"NOISE?\r\n")
            ser.flush()
            cmd_end = time.time()
            cmd_time = (cmd_end - cmd_start) * 1000

            # Measure time to first byte
            first_byte_start = time.time()
            first_byte = ser.read(1)
            first_byte_time = (time.time() - first_byte_start) * 1000

            if not first_byte:
                print("⚠️  No response received!")
                time.sleep(2)
                continue

            # Read remaining bytes and measure per-byte timing
            frame = bytearray(first_byte)
            byte_timings = [first_byte_time]

            for i in range(7):
                byte_start = time.time()
                byte = ser.read(1)
                byte_time = (time.time() - byte_start) * 1000
                if byte:
                    frame.extend(byte)
                    byte_timings.append(byte_time)

            total_response_time = (time.time() - cmd_start) * 1000

            # Performance metrics
            print("\n[Performance Metrics]")
            print(f"  Command send time:      {cmd_time:.2f} ms")
            print(f"  Time to first byte:     {first_byte_time:.1f} ms")
            print(f"  Total response time:    {total_response_time:.1f} ms")
            print(f"  Bytes received:         {len(frame)}")

            if len(byte_timings) == 8:
                print("\n[Per-Byte Timing (ms)]")
                for idx, bt in enumerate(byte_timings):
                    print(f"    Byte {idx}: {bt:.2f} ms")
                print(f"  Sum of byte times:      {sum(byte_timings):.1f} ms")
                print(f"  Avg time per byte:      {sum(byte_timings) / 8:.2f} ms")
                print(
                    f"  Min/Max byte time:      {min(byte_timings):.2f} / {max(byte_timings):.2f} ms"
                )

            # Track statistics
            response_times.append(total_response_time)
            byte_times.extend(byte_timings)

            # Data analysis
            print("\n[Data Analysis]")
            print(f"  Raw hex:    {frame.hex()}")
            print(f"  Raw bytes:  {[hex(b) for b in frame]}")

            if len(frame) == 8:
                result = noise.parse_noise_response(bytes(frame))
                if result:
                    print(
                        f"\n✅ LAeq: {result['LAeq']:.1f} dB | LZeq: {result['LZeq']:.1f} dB"
                    )
                else:
                    print("\n❌ Invalid frame format")
            else:
                print("\n⚠️  Incomplete frame")

            # Running statistics
            if len(response_times) > 1:
                print(f"\n[Running Statistics - {len(response_times)} samples]")
                print(
                    f"  Avg response time:  {sum(response_times) / len(response_times):.1f} ms"
                )
                print(f"  Min response time:  {min(response_times):.1f} ms")
                print(f"  Max response time:  {max(response_times):.1f} ms")
                print(f"  Suggested timeout:  {max(response_times) * 1.5:.0f} ms")

            time.sleep(2)

    except serial.SerialException as e:
        print(f"\n[Noise Test] ❌ Serial error: {e}")
    except KeyboardInterrupt:
        print(f"\n\n{'=' * 60}")
        print("[Noise Test] Stopped by user")
        if response_times:
            print(f"\n[Final Statistics - {len(response_times)} samples]")
            print(
                f"  Average response time: {sum(response_times) / len(response_times):.1f} ms"
            )
            print(f"  Min response time:     {min(response_times):.1f} ms")
            print(f"  Max response time:     {max(response_times):.1f} ms")
            print(f"  Recommended timeout:   {max(response_times) * 1.5:.0f} ms")
    except Exception as e:
        print(f"\n[Noise Test] ❌ Error: {e}")
        import traceback

        traceback.print_exc()
    finally:
        if "ser" in locals() and ser.is_open:
            ser.close()
            print("\n[Noise Test] Serial port closed")
