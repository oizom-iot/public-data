"""Interactive quality-control testing module for Oizom hardware boards.

Provides a CLI-driven test harness used on the factory floor to validate
individual sensors, communication interfaces, and full-board assemblies
(motherboard, DNS, and HOLO allocation). Each sensor test instantiates
the relevant ``OzWrapper`` (for example :class:`OzWrapper.OzTemp.OzTemp`
or :class:`OzWrapper.OzDust.OzDust`), reads several samples, validates
the output, and reports pass/fail status. Results are rendered as Rich
tables in the console and persisted to ``QC/app.log``. The harness also
exposes diagnostic helpers for power-rail reboots, firmware uploads,
WiFi management, and direct shell access.

Example:
    Run the interactive menu from the project root::

        $ python -m QC.QC  # doctest: +SKIP

    Or invoke individual sensor tests via CLI flags::

        $ python -m QC.QC --sht --bme --dust  # doctest: +SKIP

Note:
    This module is hardware-only and expects an Oizom board to be
    connected to ``/dev/ttyACM0`` (SAMD), the I2C bus, and the MCP
    I/O expander. Most methods will fail or hang on a development
    workstation. Authors: Joy Jacob, Bhavya Desai.
"""

__author__ = "Joy Jacob | Bhavya Desai"
__version__ = "2.3"
__status__ = "QC"
__date__ = "04/09/2024"

import json
import logging
import os
import subprocess
import threading
import time
from queue import Queue

import Adafruit_MCP4725
import requests
import serial
from drivers.gpio import gpio
from drivers.HMI.HMI import HMI
from drivers.LORAE5.LORAE5 import LORAE5
from drivers.MCP230XX import MCP230XX
from drivers.Noise.Noise import Noise
from drivers.OizomNoiseV2 import OizomNoiseV2
from drivers.Rain import Rain
from drivers.SVANTEK import SVANTEK
from drivers.TM1637.TM1637 import TM1637Decimal
from drivers.TM1638.TM1638 import TM1638
from drivers.Wind.Wind import Wind
from Network import Network
from OzWrapper.OzBattery import OzBattery
from OzWrapper.OzCO2 import OzCO2
from OzWrapper.OzDust import OzDust
from OzWrapper.OzDustCal.OzDustCal import OzDustCal
from OzWrapper.OzFlood import OzFlood
from OzWrapper.OzGPS import OzGPS
from OzWrapper.OzLightning import OzLightning
from OzWrapper.OzNoise import OzNoise
from OzWrapper.OzOGS import OzOGS
from OzWrapper.OzOSP import OzOSP
from OzWrapper.OzRain import OzRain
from OzWrapper.OzRGB import OzRGB
from OzWrapper.OzSamd import OzSamd
from OzWrapper.OzSoil import OzSoil
from OzWrapper.OzSystem import OzSystem
from OzWrapper.OzTemp import OzTemp
from OzWrapper.OzUVLight import OzUVLight
from OzWrapper.OzWind import OzWind
from OzWrapper.OzWindV2 import OzWindV2
from rich import print as rprint
from rich.console import Console
from rich.progress import track
from rich.table import Table

OIZOM_MANAGER = "http://manager.oizom.com"
OIZOM_SOCKET = "http://socket.oizom.com"
HOLO_API = "/v2/qc/init/"
QC_API = "/qc/sensor/data"
serial_port = "/dev/ttyACM0"


class QC:
    """Interactive quality-control test harness for Oizom hardware.

    Provides command-driven sensor tests (SHT31, BME280, dust, OGS, etc.)
    grouped into motherboard (MB), DNS, and HOLO test suites. Each test
    instantiates the appropriate OzWrapper, reads sensor data, validates
    it, and reports pass/fail. Operators interact through
    :meth:`.get_input` or by supplying CLI flags parsed by
    :meth:`.parse_arguments`.

    Attributes:
        config (dict): Device configuration dictionary (unused in QC mode).
        sensor (object): Currently active sensor wrapper instance under test.
        MCP (drivers.MCP230XX.MCP230XX): MCP23017 I/O expander instance
            used for fan and siren control.
        network (Network.Network): Network connectivity monitor.
        network_status (queue.Queue): Queue carrying the current LED
            status code consumed by the RGB LED thread.
        qcStatus (bool): Overall QC pass/fail flag.
        qcFlag (bool): Whether the QC session is active.
        mbFlag (bool): Whether the motherboard test suite has completed.
        dnsFlag (bool): Whether the DNS test suite has completed.
        sensorStatusList (dict): Mapping of sensor short names to pass
            (``1``) / fail (``0``).
        qcCommands (dict): Mapping of command codes to human-readable
            test names, see :obj:`QC.qcCommands`.
        rdCommands (dict): Mapping of hidden/diagnostic command codes to
            descriptions, see :obj:`QC.rdCommands`.
        argsParse (dict): CLI long-option to command-code mapping.
        ledStatus (tuple): Human-readable strings for each LED state.

    Example:
        Construct a QC instance and dispatch an SHT31 test::

            >>> qc = QC()  # doctest: +SKIP
            >>> qc.indirect("d")  # doctest: +SKIP
    """

    # Class #QC variables
    config = {}

    sensor = None
    MCP = None
    network = Network(timeout=6)
    network_status = Queue(2)
    Led_rgb = OzRGB
    samd = None
    oztemp = None
    ozsoil = None
    ozbatt = None
    ozgps = None
    ozdust = None
    ozogs = None
    ozuvlight = None
    ozflood = None
    ozsystem = None
    tm1637 = None
    ozosp = None
    co2 = None

    gps_avail = False
    init_flag = 0
    init_value = {}
    i = ""  # Empty string for QC indirect method
    command = None
    qcStatus = False
    qcFlag = True  # if true, QC file will be Executed.
    mbFlag = False  # if true, MotherBoard testing is completed.
    dnsFlag = False  # if true, DNS testing is completed.
    _timeout_ = 5
    TIMEOUT = _timeout_ * 10000

    masterV2 = ["svantek", "wind2", "rain2"]

    ledStatus = (
        "Disconnected - Green blinking",
        "Connected - Cyan Breathing",
        "No Simcard - Blue Blinking",
        "Sending Data - Magenta Fast Blink",
        "Getting Config - Yellow Fast Blink",
        "Some Bug Found - Red Blink",
        "init success - Purple Blink",
        "OGS Allocation - Neon Orange Blinking",
        "QC MODE - Blue Breathing",
    )
    sensorStatusList = {
        "sht": 0,
        "bme": 0,
        "aht20": 0,
        "sen66": 0,
        "batt": 0,
        "dust": 0,
        "ogs": 0,
        "lightuv": 0,
        "fan": 0,
        "noise": 0,
        "wind": 0,
        "rain": 0,
        "rtc": 0,
        "CO2": 0,
    }
    clearDisplay = [0, 0, 0, 0, 0, 0]  # (Clear display)
    samdWatchdog = {
        "samd": {
            "watchdog": [
                {
                    "en": 1,
                    "pn": 1,
                    "gpio": {"baud": 115200, "port": "/dev/ttyACM0"},
                    "timerValue": 300,
                }
            ]
        }
    }

    server = os.getenv(
        "NETWORKMANAGER_URL", "http://172.20.0.1:8084"
    )  # # Hardware services <-> Gateway IP Bridge in-between <->  Network manager
    status_api = os.getenv("STATUS_API", "/network/status")
    samd_api = os.getenv("SAMD_API", "/samd/firmware/update")
    scan_wifi_api = os.getenv("SCAN_WIFI_API", "/network/wifi/scan")
    wifi_status_api = os.getenv("WIFI_STATUS_API", "/network/wifi/status")
    generate_hotspot_api = os.getenv("GENERATE_HOTSPOT_API", "/network/wifi/createap")
    connect_Oizom_api = "/network/wifi/connect"
    SSID = "OIZOM"
    password = "polludrone"
    response = None
    qcBypass = False

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOjIwODksIm9yZ0lkIjoiT1pfQVBJUyIsInVzZXJFbWFpbCI6ImFwaXNAb2l6b20uY29tIiwiaWF0IjoxNjExNTU5MDE5LCJleHAiOjE4MzIzMTEwMTksImlzcyI6ImxiVzRvOUlUVTZTcHZXekRyeWV3TWVqTjF6cXM4NkVZIn0.8W9fVYBfWMZ931WlfEoL-dFIl9zdTNbMyb3A3dzjags",
    }

    mbCommands = {
        "LED test": "i",
        "RTC Test": "qg",
        "SHT test": "d",
        "BME test": "e",
        "AHT20 test": "ht",
        "Battery test": "f",
        "Light-UV test": "k",
        "Fan test": "l",
    }
    helium_mbCommands = {
        "LED test": "i",
        "RTC Test": "qg",
        "SHT test": "d",
        "AHT20 test": "ht",
        "Light-UV test": "k",
        "Fan test": "l",
    }
    dnsCommands = {
        "LED test": "i",
        "SHT test": "d",
        "BME test": "e",
        "AHT20 test": "ht",
        "Battery test": "f",
        "Light-UV test": "k",
        "Fan test": "l",
        "Cubic Test": "h",
        "Noise Test": "n",
        "Wind Test": "o",
    }
    helium_dnsCommands = {
        "LED test": "i",
        "SHT test": "d",
        "Light-UV test": "k",
        "Fan test": "l",
        "Cubic Test": "h",
    }
    holoCommands = {
        "LED test": "i",
        "SHT test": "d",
        "BME test": "e",
        "AHT20 test": "ht",
        "Battery test": "f",
        "Light-UV test": "k",
        "Fan test": "l",
        "Cubic Test": "h",
        "Noise Test": "n",
        "Wind Test": "o",
        "Rain Test": "p",
        "Display Test": "y",
        "Siren Test": "z",
        "OGS Test": "j",
        "RTC Test": "qg",
        "CO2 Test": "ac",
        "AQBOT I2C CO2 Test": "aqad",
    }
    helium_holoCommands = {
        "LED test": "i",
        "SHT test": "d",
        "Light-UV test": "k",
        "Fan test": "l",
        "Cubic Test": "h",
        "Display Test": "hy",
        "Siren Test": "hz",
        "OGS Test": "j",
        "RTC Test": "qg",
        "CO2 Test": "ac",
    }
    i2cCommands = {
        "SHT test": "d",
        "BME test": "e",
        "AHT20 test": "ht",
        "SEN66 test": "se",
        "Battery test": "f",
        "Light-UV test": "k",
    }
    uartComands = {"Cubic Test": "h", "OGS Test": "j"}
    samdCommands = {"Noise Test": "n", "Wind Test": "o", "Rain Test": "p"}

    argsParse = {
        "--sht": "d",
        "--bme": "e",
        "--aht20": "ht",
        "--pt1000": "pt",
        "--ospVisibility": "vs",
        "--ospWind": "o3",
        "--sen66": "se",
        "--battery": "f",
        "--gps": "g",
        "--dust": "h",
        "--led": "i",
        "--ogs": "j",
        "--lightuv": "k",
        "--caldust": "cd",
        "--fan": "l",
        "--network": "m",
        "--noise": "n",
        "--wind": "o",
        "--rain": "p",
        "--flood": "r",
        "--relay": "s",
        "--light": "t",
        "--uv": "u",
        "--visibility": "v",
        "--surface": "w",
        "--display": "y",
        "--siren": "z",
        "--co2elt": "ac",
        "--co2scd": "ad",
        "--aqbotco2scd": "aqad",
        "--sensorlist": "qz",
        "--checki2c": "qy",
        "--checkuart": "qx",
        "--checksamd": "qw",
        "--checkgsm": "qv",
        "--s1sg": "qu",
        "--smogs": "qs",
        "--nnogs": "qr",
        "--flooduart": "qp",
        "--uploadsamd": "qo",
        "--reboot3v3": "qn",
        "--reboot5v": "qm",
        "--rebootsystem": "ql",
        "--help": "help",
        "--athp": "ae",
        "--athp2": "ae2",
        "--allOGS": "af",
        "--allOGSV2": "af2",
        "--checkrtc": "qg",
        "--heliumsiren": "hz",
    }
    qcCommands = {
        "a": "MotherBoard Test (Zulu-Board)",
        "ha": "Helium MotherBoard Test",
        "b": "DNS Test (Dust-Noise-&Sensors)",
        "hb": "Helium DNS Test",
        "c": "HOLO Test (with Ethernet)",
        "hc": "Helium HOLO Test (with Ethernet)",
        "d": "SHT31 Test",
        "e": "BME280 Test",
        "ht": "AHT20 Test",
        "pt": "PT1000 Test",
        "vs": "MODBUS Visibility Test",
        "o3": "MODBUS Wind Test",
        "se": "SEN66 Test",
        "f": "Battery Test",
        "g": "GPS Test",
        "h": "Dust Test",
        "h2": "Dust Test 2",  # Cubic PM6303 modbus industrial dust sensor test
        "i": "LED Test",
        "j": "OGS Test",
        "k": "Light-UV35 Test",
        "cd": "Dust Calibration",
        "l": "Fan Test",
        "m": "Network Test",
        "n": "Noise Test",
        "n2": "Noise2 Test",  # Noise which is direct connected to CM4
        "n3": "Helium Noise Test",  # Helium Noise which is direct connected to CM4
        "o": "Wind Test",
        "o2": "Wind2 Test",  # SDI12 Wind sensor
        "o3": "Wind3 Test",  # Modbus Wind sensor
        "so1": "Soil1 Test",  # SEN0600 Soil sensor test
        "so2": "Soil2 Test",  # NIUBOL 8 parameter Soil sensor test
        "so3": "Soil3 Test",  # NIUBOL 3 parameter Soil sensor test
        "p": "Rain Test",
        "p2": "Rain2 Test",
        "q": "QC checklist",
        "r": "Flood Test",
        "s": "Relay Test",
        "st": "Svantek Noise Test",
        "t": "Light Testing - TSL2591",
        "u": "UV35 Test - SI1147",
        "v": "Visibility sensor Test",
        "w": "Surface Temperature Test",
        "y": "Display Test",
        "hy": "Display Test for Helium TM1638",
        "z": "Siren (Buzzer + LED) Test",
        "hz": "Helium Siren (Buzzer + LED) Test",
        "x": "EXIT the QC",
        "jj": "Other Important Commands",
        "ac": "CO2 Test - ELT",
        "ad": "CO2 Test - SCD",
        "aqad": "AQBOT I2C CO2 Test",
        "ae": "ATHP test",
        "ae2": "ATHP2 test",
        "af": "All positions OGS test",
        "af2": "All positions OGSV2 test",
        "ag": "UV 36 - LTR390 test",
        "ah": "ATH Test",
        "e5": "Grove LoRa E5 DevEUI and AppEUI Check",
        "be": "Beacon test",
        "as": "AS3935 Test",
    }
    rdCommands = {
        "qz": "checking the sensor List ",
        "qy": "checking all i2c sensors",
        "qx": "checking all UART sensors",
        "qw": "checking all SAMD sensors",
        "qv": "checking things related with GSM",
        "qu": "CPU temperature & GSM signal strength",
        "qt": "checking OGS at individual positions",
        "qp": "checking Flood UART sensor",
        "jj": "Hidden Commands",
        "qo": "Upload SAMD Firmware",
        "qn": "Reboot 3v3",
        "qm": "Reboot 5v",
        "ql": "Reboot System",
        "qk": "Display Message",
        "qr": "checking Nevada OGS sensors",
        "qs": "checking Semeatech OGS sensors ",
        "qj": "Scan WiFi",
        "qh": "Enable Hotspot of device",
        "qi": "Connect with Oizom WiFi",
        "qg": "Check RTC",
        "qf": "QC Bypass flag",
        "qe": "check 4-20 module",
        "qd": "HMI Test",
        "exe": "Open Terminal to execute direct commands",
    }
    # extraCommands = {
    #     "bh" : "extraCommands"
    # }
    argsCommand = {
        "--sht": "SHT31 Test",
        "--bme": "BME280 Test",
        "--aht20": "AHT20 Test",
        "--pt1000": "PT1000 Test",
        "--ospVisibility": "MODBUS Visibility Test",
        "--ospWind": "MODBUS Wind Test",
        "--sen66": "SEN66 Test",
        "--battery": "Battery Test",
        "--gps": "GPS Test",
        "--dust": "Dust Test",
        "--led": "LED Test",
        "--ogs": "OGS Test",
        "--lightuv": "Light-UV35 Test",
        "--fan": "Fan Test",
        "--network": "Network Test",
        "--noise": "Noise Test",
        "--wind": "Wind Test",
        "--rain": "Rain Test",
        "--flood": "Flood Test",
        "--relay": "Relay Test",
        "--light": "Light Testing - TSL2591",
        "--uv": "UV35 Test - SI1147",
        "--visibility": "Visibility sensor Test",
        "--surface": "Surface Temperature Test",
        "--display": "Display Test for AQBOT",
        "--siren": "Siren (Buzzer + LED) Test",
        "--sensorlist": "checking the sensor List ",
        "--checki2c": "checking all i2c sensors",
        "--checkuart": "checking all UART sensors",
        "--checksamd": "checking all SAMD sensors",
        "--checkgsm": "checking things related with GSM",
        "--s1sg": "CPU temperature & GSM signal strength",
        "--ogs": "checking OGS at individual positions",
        "--smogs": "checking Semeatech OGS sensors",
        "--nnogs": "checking Nevada OGS sensors",
        "--flooduart": "checking Flood UART sensor",
        "--uploadsamd": "Upload SAMD Firmware",
        "--reboot3v3": "Reboot 3v3",
        "--reboot5v": "Reboot 5v",
        "--rebootsystem": "Reboot System",
        "--athp": "ATHP test",
        "--athp2": "ATHP2 test",
        "--allOGS": "All positions OGS test for sensorboard",
        "--allOGSV2": "All positions OGSV2 test for sensorboard",
        "--checkrtc": "Check RTC",
    }
    qcSensorList = {
        "d": "sht31",
        "e": "bme280",
        "ht": "aht20",
        "pt": "pt1000",
        "vs": "MODBUS_vis",
        "o3": "MODBUS_wind",
        "se": "sen66",
        "f": "batt",
        "g": "gps",
        "h": "dust",
        "h2": "dust2",
        "j": "ogs",
        "k": "lightuv",
        "n": "noise",
        "n2": "noise2",
        "n3": "helium_noise",
        "st": "svantek",
        "o": "wind",
        "o2": "wind2",
        "o3": "wind3",
        "so1": "soil1",
        "so2": "soil2",
        "so3": "soil3",
        "p": "rain",
        "p2": "rain2",
        "r": "flood",
        "t": "light",
        "u": "uv35",
        "v": "visibility",
        "w": "surface",
        "qu": "system",
        "qp": "floodUart",
        "ac": "elt_CO2",
        "ad": "scd_CO2",
        "aqad": "aqbot_scd_CO2",
        "ae": "athp",
        "ae2": "athp2",
        "af": "allOGS",
        "af2": "allOGSV2",
        "ag": "uv36",
        "ah": "sht31",
        "as": "AS3935",
    }

    sht31 = {
        "class": "oztemp",
        "init": [
            {
                "en": 1,
                "pn": 25,
                "parameters": [
                    {"cr": 0, "pm": 1, "pn": 25, "sc": "temp", "se": 100},
                    {"cr": 0, "pm": 2, "pn": 25, "sc": "hum", "se": 100},
                ],
            }
        ],
        "sensor": "sht",
    }
    bme280 = {
        "class": "oztemp",
        "init": [
            {
                "en": 1,
                "pn": 24,
                "parameters": [
                    {"cr": 0, "pm": 1, "pn": 24, "sc": "t1", "se": 100},
                    {"cr": 0, "pm": 2, "pn": 24, "sc": "t2", "se": 100},
                    {"cr": 0, "pm": 3, "pn": 24, "sc": "pr", "se": 100},
                ],
            }
        ],
        "sensor": "bme",
    }
    aht20 = {
        "class": "oztemp",
        "init": [
            {
                "en": 1,
                "pn": 29,
                "parameters": [
                    {"cr": 0, "pm": 1, "pn": 29, "sc": "t1", "se": 100},
                    {"cr": 0, "pm": 2, "pn": 29, "sc": "t2", "se": 100},
                ],
            }
        ],
        "sensor": "aht20",
    }
    batt = {
        "class": "ozbatt",
        "init": [
            {
                "pn": 3,
                "en": 1,
                "parameters": [
                    {"pm": 1, "se": 100, "cr": 0, "sc": "volt"},
                    {"pm": 2, "se": 100, "cr": 0, "sc": "current"},
                    {"pm": 3, "se": 100, "cr": 0, "sc": "bs"},
                ],
            }
        ],
        "sensor": "batt",
    }
    gps = {
        "class": "ozgps",
        "init": [
            {
                "en": 1,
                "pn": 30,
                "parameters": [{"pm": 1, "sc": "lat"}, {"pm": 2, "sc": "lon"}],
            }
        ],
        "sensor": "gps",
    }
    dust2 = {
        "class": "ozdust",
        "init": [
            {
                "en": 1,
                "pn": 19,
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "p1", "se": 60},
                    {"cr": 0, "pm": 2, "sc": "p2", "se": 100},
                    {"cr": 0, "pm": 3, "sc": "p3", "se": 60},
                    {"cr": 0, "pm": 4, "sc": "p4", "se": 100},
                    {"cr": 0, "pm": 5, "sc": "p5", "se": 100},
                    {"cr": 0, "pm": 6, "sc": "flow", "se": 100},
                ],
            }
        ],
        "sensor": "dust2",
    }
    dust = {
        "class": "ozdust",
        "init": [
            {
                "en": 1,
                "pn": 18,
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "p1", "se": 60},
                    {"cr": 0, "pm": 2, "sc": "p2", "se": 100},
                    {"cr": 0, "pm": 3, "sc": "p3", "se": 60},
                    {"cr": 0, "pm": 4, "sc": "p4", "se": 100},
                ],
            }
        ],
        "sensor": "dust",
    }
    sen66 = {
        "class": "ozdust",
        "init": [
            {
                "en": 1,
                "pn": 13,
                "lb": "",
                "sensorId": "",
                "gpio": {"pos": 0},
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "p1", "se": 100},
                    {"cr": 0, "pm": 2, "sc": "p2", "se": 100},
                    {"cr": 0, "pm": 3, "sc": "p3", "se": 100},
                    {"cr": 0, "pm": 4, "sc": "p5", "se": 100},
                    {"cr": 0, "pm": 5, "sc": "temp", "se": 100},
                    {"cr": 0, "pm": 6, "sc": "hum", "se": 100},
                    {"cr": 0, "pm": 7, "sc": "v2", "se": 100},
                    {"cr": 0, "pm": 8, "sc": "r1", "se": 100},
                    {"cr": 0, "pm": 9, "sc": "g1", "se": 100},
                ],
            }
        ],
        "sensor": "dust",
    }
    ogs = {
        "class": "ozogs",
        "init": [
            {
                "en": 1,
                "pn": 101,
                "gpio": {"pos": 0, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g11", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g12", "se": 100},
                ],
            }
        ],
        "sensor": "ogs",
    }
    allOGS = {
        "class": "ozogs",
        "init": [
            {
                "en": 1,
                "pn": 101,
                "gpio": {"pos": 0, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g11", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g12", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 101,
                "gpio": {"pos": 1, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g21", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g22", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 101,
                "gpio": {"pos": 2, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g31", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g32", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 101,
                "gpio": {"pos": 3, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g41", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g42", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 101,
                "gpio": {"pos": 4, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g51", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g52", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 101,
                "gpio": {"pos": 5, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g61", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g62", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 101,
                "gpio": {"pos": 6, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g71", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g72", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 101,
                "gpio": {"pos": 7, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g81", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g82", "se": 100},
                ],
            },
        ],
        "sensor": "ogs",
    }
    allOGSV2 = {
        "class": "ozogs",
        "init": [
            {
                "en": 1,
                "pn": 141,
                "gpio": {"pos": 0, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g11", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g12", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 141,
                "gpio": {"pos": 1, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g21", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g22", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 141,
                "gpio": {"pos": 2, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g31", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g32", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 141,
                "gpio": {"pos": 3, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g41", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g42", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 141,
                "gpio": {"pos": 4, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g51", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g52", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 141,
                "gpio": {"pos": 5, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g61", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g62", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 141,
                "gpio": {"pos": 6, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g71", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g72", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 141,
                "gpio": {"pos": 7, "baud": 115200, "port": "/dev/ttyAMA0"},
                "parameters": [
                    {"ch": 0, "cr": 0, "pm": 1, "sc": "g81", "se": 100},
                    {"ch": 1, "cr": 0, "pm": 2, "sc": "g82", "se": 100},
                ],
            },
        ],
        "sensor": "ogs",
    }
    lightuv = {
        "class": "ozuvlight",
        "init": [
            {
                "pn": 32,
                "en": 1,
                "parameters": [{"pm": 1, "se": 100, "cr": 0, "sc": "light"}],
            },
            {
                "pn": 35,
                "en": 1,
                "parameters": [{"pm": 2, "se": 100, "cr": 0, "sc": "uv"}],
            },
        ],
        "sensor": "lightuv",
    }
    noise = {
        "class": "oznoise",
        "init": [
            {
                "en": 1,
                "pn": 41,
                "gpio": {"baud": 115200, "port": "/dev/ttyACM0"},
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "leq", "se": 100},
                    {"cr": 0, "pm": 2, "sc": "lmax", "se": 100},
                    {"cr": 0, "pm": 3, "sc": "lmin", "se": 100},
                ],
            }
        ],
        "sensor": "noise",
    }
    wind = {
        "class": "ozwind",
        "init": [
            {
                "en": 1,
                "pn": 211,
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "wd", "se": 100},
                    {"cr": 0, "pm": 2, "sc": "ws", "se": 100},
                ],
            }
        ],
        "sensor": "wind",
    }
    rain = {
        "class": "ozrain",
        "init": [
            {
                "en": 1,
                "pn": 61,
                "gpio": {"pin": 10, "baud": 115200, "port": "/dev/ttyACM0"},
                "parameters": {"pm": 1, "sc": "rain"},
            }
        ],
        "sensor": "rain",
    }
    noise2 = {
        "class": "noise2",
        "init": [
            {
                "en": 1,
                "pn": 41,
                "gpio": {"baud": 115200, "port": "/dev/ttyACM0"},
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "leq", "se": 100},
                    {"cr": 0, "pm": 2, "sc": "lmax", "se": 100},
                    {"cr": 0, "pm": 3, "sc": "lmin", "se": 100},
                ],
            }
        ],
        "sensor": "noise2",
    }
    wind2 = {
        "class": "wind2",
        "init": [
            {
                "en": 1,
                "pn": 211,
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "wd", "se": 100},
                    {"cr": 0, "pm": 2, "sc": "ws", "se": 100},
                ],
            }
        ],
        "sensor": "wind2",
    }
    rain2 = {
        "class": "rain2",
        "init": [
            {
                "en": 1,
                "pn": 61,
                "parameters": [{"pm": 1, "sc": "rain"}],
            }
        ],
        "sensor": "rain2",
    }
    flood = {
        "class": "ozflood",
        "init": [
            {
                "en": 1,
                "pn": 71,
                "parameters": [{"cr": 0, "pm": 1, "sc": "flood", "se": 100}],
            }
        ],
        "sensor": "flood",
    }
    light = {
        "class": "ozuvlight",
        "init": [
            {
                "pn": 32,
                "en": 1,
                "parameters": [{"pm": 1, "se": 100, "cr": 0, "sc": "light"}],
            }
        ],
        "sensor": "light",
    }
    uv35 = {
        "class": "ozuvlight",
        "init": [
            {
                "pn": 35,
                "en": 1,
                "parameters": [{"pm": 2, "se": 100, "cr": 0, "sc": "uv"}],
            }
        ],
        "sensor": "uv",
    }
    uv36 = {
        "class": "ozuvlight",
        "init": [
            {
                "en": 1,
                "pn": 36,
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "uv1", "se": 100},
                    {"cr": 0, "pm": 2, "sc": "uv2", "se": 100},
                    {"cr": 0, "pm": 3, "sc": "uv3", "se": 100},
                    {"cr": 0, "pm": 4, "sc": "uv4", "se": 100},
                ],
            }
        ],
        "sensor": "uv",
    }
    visibility = {
        "class": "samd",
        "init": {
            "visibility": [
                {
                    "en": 1,
                    "pn": 311,
                    "parameters": [{"cr": 0, "pm": 1, "sc": "vs", "se": 100}],
                }
            ]
        },
        "sensor": "visibility",
    }
    surface = {
        "class": "samd",
        "init": {
            "surface": [
                {
                    "en": 1,
                    "pn": 411,
                    "parameters": [{"cr": 0, "pm": 1, "sc": "st", "se": 100}],
                }
            ]
        },
        "sensor": "surface",
    }
    system = {
        "class": "ozsystem",
        "init": [
            {"en": 1, "pn": 10, "parameters": [{"pm": 1, "sc": "s1"}]},
            {
                "en": 1,
                "pn": 11,
                "gpio": {"baud": 115200, "port": "/dev/ttyUSB2"},
                "parameters": [{"pm": 1, "sc": "sg"}],
            },
        ],
        "sensor": "system",
    }
    floodUart = {
        "class": "ozflood",
        "init": [
            {
                "en": 1,
                "pn": 72,
                "parameters": [{"cr": 0, "pm": 1, "sc": "flood", "se": 100}],
            }
        ],
        "sensor": "flood",
    }
    elt_CO2 = {
        "class": "ozco2",
        "init": [
            {
                "en": 1,
                "pn": 53,
                "parameters": [{"cr": 0, "pm": 1, "sc": "co2", "se": 100}],
            }
        ],
        "sensor": "CO2",
    }
    scd_CO2 = {
        "class": "ozco2",
        "init": [
            {
                "en": 1,
                "pn": 54,
                "parameters": [{"cr": 0, "pm": 1, "sc": "co2", "se": 100}],
            }
        ],
        "sensor": "CO2",
    }
    aqbot_scd_CO2 = {
        "class": "ozco2",
        "init": [
            {
                "en": 1,
                "pn": 54,
                "gpio": {"pos": 1},
                "parameters": [{"cr": 0, "pm": 1, "sc": "co2", "se": 100}],
            }
        ],
        "sensor": "CO2",
    }
    athp = {
        "class": "oztemp",
        "init": [
            {
                "en": 1,
                "pn": 25,
                "parameters": [
                    {"cr": 0, "pm": 1, "pn": 25, "sc": "temp", "se": 100},
                    {"cr": 0, "pm": 2, "pn": 25, "sc": "hum", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 26,
                "parameters": [{"cr": 0, "pm": 3, "pn": 26, "sc": "pr", "se": 100}],
            },
        ],
        "sensor": "bme",
    }
    athp2 = {
        "class": "oztemp",
        "init": [
            {
                "en": 1,
                "pn": 25,
                "parameters": [
                    {"cr": 0, "pm": 1, "pn": 25, "sc": "temp", "se": 100},
                    {"cr": 0, "pm": 2, "pn": 25, "sc": "hum", "se": 100},
                ],
            },
            {
                "en": 1,
                "pn": 28,
                "parameters": [
                    {"cr": 0, "pm": 1, "pn": 28, "sc": "temp", "se": 100},
                    {"cr": 0, "pm": 3, "pn": 28, "sc": "pr", "se": 100},
                ],
            },
        ],
        "sensor": "bme",
    }
    lightning = {
        "class": "ozlightning",
        "init": [
            {
                "en": 1,
                "pn": 39,
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "lst", "se": 100},
                    {"cr": 0, "pm": 2, "sc": "lsd", "se": 100},
                ],
            }
        ],
        "sensor": "lightning",
    }
    pt1000 = {
        "class": "ozosp",
        "init": [
            {
                "en": 1,
                "pn": 151,
                "slave_id": 1,
                "debug": 0,
                "gpio": {"port": "/dev/ttyAMA2", "baudrate": 9600, "parity": "N"},
                "parameters": [
                    {
                        "register": 2,
                        "fn_code": 3,
                        "count": 1,
                        "parsing_type": 0,
                        "se": 1,
                        "cr": 0,
                        "sc": "pt_temp",
                    }
                ],
            }
        ],
        "sensor": "pt1000",
    }
    MODBUS_vis = {
        "class": "ozosp",
        "init": [
            {
                "en": 1,
                "pn": 132,
                "slave_id": 1,
                "debug": 1,
                "gpio": {"port": "/dev/ttyAMA2", "baudrate": 9600, "parity": "N"},
                "parameters": [
                    {
                        "register": 0,
                        "fn_code": 3,
                        "count": 5,
                        "parsing_type": 0,
                        "se": 100,
                        "cr": 0,
                        "sc": "vis",
                    }
                ],
            }
        ],
        "sensor": "MODBUS_vis",
    }
    MODBUS_wind = {
        "class": "ozosp",
        "init": [
            {
                "en": 1,
                "pn": 131,
                "slave_id": 1,
                "debug": 1,
                "gpio": {"port": "/dev/ttyAMA2", "baudrate": 9600, "parity": "N"},
                "parameters": [
                    {
                        "register": 2,
                        "fn_code": 3,
                        "count": 2,
                        "parsing_type": 1,
                        "se": 100,
                        "cr": 0,
                        "sc": "ws",
                    },
                    {
                        "register": 1,
                        "fn_code": 3,
                        "count": 1,
                        "parsing_type": 0,
                        "se": 100,
                        "cr": 0,
                        "sc": "wd",
                    },
                ],
            }
        ],
        "sensor": "MODBUS_wind",
    }
    svantek = {
        "class": "svantek",
        "init": {
            "en": 1,
            "pn": 43,
            "parameters": [
                {"cr": 0, "pm": 1, "sc": "n11", "se": 100},
                {"cr": 0, "pm": 2, "sc": "n21", "se": 100},
                {"cr": 0, "pm": 3, "sc": "n31", "se": 100},
            ],
        },
        "sensor": "svantek",
    }
    wind2 = {
        "class": "wind2",
        "init": [
            {
                "en": 1,
                "pn": 211,
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "wd", "se": 100},
                    {"cr": 0, "pm": 2, "sc": "ws", "se": 100},
                ],
            }
        ],
        "sensor": "wind2",
    }
    wind3 = {
        "class": "wind3",
        "init": [
            {
                "en": 1,
                "pn": 214,
                "gpio": {"port": "/dev/ttyAMA2"},
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "wd", "se": 100},
                    {"cr": 0, "pm": 2, "sc": "ws", "se": 100},
                ],
            }
        ],
        "sensor": "wind3",
    }

    rain2 = {
        "class": "rain2",
        "init": [
            {
                "en": 1,
                "pn": 61,
                "parameters": [{"pm": 1, "sc": "rain"}],
            }
        ],
        "sensor": "rain2",
    }

    soil1 = {
        "class": "ozsoil",
        "init": [
            {
                "en": 1,
                "pn": 111,
                "gpio": {"port": "/dev/ttyAMA2"},
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "so2", "se": 100},
                    {"cr": 0, "pm": 2, "sc": "so3", "se": 100},
                ],
            }
        ],
        "sensor": "soil1",
    }

    soil2 = {
        "class": "ozsoil",
        "init": [
            {
                "en": 1,
                "pn": 112,
                "gpio": {"port": "/dev/ttyAMA2"},
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "so1", "se": 100},
                    {"cr": 0, "pm": 2, "sc": "so2", "se": 100},
                    {"cr": 0, "pm": 3, "sc": "so3", "se": 100},
                    {"cr": 0, "pm": 4, "sc": "so4", "se": 100},
                    {"cr": 0, "pm": 5, "sc": "so5", "se": 100},
                    {"cr": 0, "pm": 6, "sc": "so6", "se": 100},
                    {"cr": 0, "pm": 6, "sc": "so7", "se": 100},
                    {"cr": 0, "pm": 6, "sc": "so8", "se": 100},
                ],
            }
        ],
        "sensor": "soil2",
    }

    soil3 = {
        "class": "ozsoil",
        "init": [
            {
                "en": 1,
                "pn": 113,
                "gpio": {"port": "/dev/ttyAMA2"},
                "parameters": [
                    {"cr": 0, "pm": 2, "sc": "so2", "se": 100},
                    {"cr": 0, "pm": 3, "sc": "so3", "se": 100},
                    {"cr": 0, "pm": 4, "sc": "so4", "se": 100},
                ],
            }
        ],
        "sensor": "soil3",
    }

    helium_noise = {
        "class": "oizomnoisev2",
        "init": [
            {
                "en": 1,
                "pn": 42,
                "debug": True,
                "rdata": False,
                "leq_detector": "fast",
                "serial": {"port": "/dev/ttyUSB3", "baudrate": 115200},
                "parameters": [
                    {"cr": 0, "pm": 1, "sc": "n11", "se": 100},
                    {"cr": 0, "pm": 2, "sc": "n21", "se": 100},
                ],
            }
        ],
        "sensor": "helium_noise",
    }

    def __init__(self) -> None:
        """Initialise the QC harness, start the RGB LED thread, and configure logging.

        Determines the host IP via ``hostname -i``, derives the gateway
        IP for the Network Manager service, seeds the
        :obj:`QC.network_status` queue, spawns the RGB LED feedback
        thread (:meth:`OzWrapper.OzRGB.OzRGB.main`), and instantiates
        the console and file loggers via :meth:`.setup_logger_console`
        and :meth:`.setup_logger_file`.

        Returns:
            None. Side effect: a daemon LED thread is started.

        Raises:
            subprocess.CalledProcessError: If ``hostname -i`` fails to
                resolve the device IP.

        Example:
            Instantiate during interactive QC bootstrap::

                >>> qc = QC()  # doctest: +SKIP

        Note:
            Requires that ``hostname -i`` returns a routable IP on the
            target device and that the SAMD serial port is reachable.
        """
        port = "8084"
        colon = ":"
        ip = subprocess.check_output(["hostname", "-i"])
        # print("The Hardware IP is: {}".format(out))
        ip = ip.decode("utf-8").strip(
            "\n"
        )  # convert response from bytes to string and drop \n
        _octets = ip.split(".")
        _octets[-1] = (
            "1"  # set the final octet to 1 (gateway), not every occurrence of the last char
        )
        gatewayIP = ".".join(_octets)
        self.server = f"{gatewayIP}{colon}{port}"
        self.network_status.put(0)
        self.network_status.put(0)
        self.led_Thread = threading.Thread(
            target=self.Led_rgb.main, args=(None, self.network_status), daemon=True
        )
        self.led_Thread.start()
        self.network_status.queue[0] = 8
        self.logger_console = self.setup_logger_console()
        self.logger_file = self.setup_logger_file()

        try:
            serial_port = "/dev/ttyACM0"
            self.samd_serial = serial.Serial(
                port=serial_port,
                baudrate=115200,
                timeout=5,
                write_timeout=3,
            )
        except Exception as e:
            print("QC", f"Error initializing serial port: {e}")

    def setup_logger_console(self) -> logging.Logger:
        """Create a logger that writes to both the console and ``QC/app.log``.

        Builds a :class:`logging.Logger` named ``[Qc]`` configured at
        ``DEBUG`` level with a :class:`logging.StreamHandler` and a
        :class:`logging.FileHandler` both formatted with the same
        ``%(asctime)s - %(name)s - %(levelname)s - %(message)s`` layout.

        Returns:
            Configured :class:`logging.Logger` instance with console and
            file handlers attached. Used throughout QC to surface
            progress to the operator while persisting a trace to disk.

        Raises:
            OSError: If the log file path is not writable.

        Example:
            Reset the logger during a test rerun::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.logger_console = qc.setup_logger_console()  # doctest: +SKIP

        Note:
            The same log file is shared with :meth:`.setup_logger_file`,
            so calling both produces two handlers writing to one file.
        """
        # Define the log file name
        log_file = "app.log"

        # Get the script directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(script_dir, log_file)

        # Create a logger for console
        logger_console = logging.getLogger("[Qc]")
        logger_console.setLevel(logging.DEBUG)  # Set the threshold level to DEBUG

        # Create a console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)

        # Create a file handler
        file_handler = logging.FileHandler(log_path, mode="a")
        file_handler.setLevel(logging.DEBUG)

        # Create a formatter and set it for the console handler
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        console_handler.setFormatter(formatter)
        file_handler.setFormatter(formatter)

        # Add the file handler to the logger
        logger_console.addHandler(file_handler)
        logger_console.addHandler(console_handler)

        # Prevent propagation to avoid logging to both handlers
        # logger_console.propagate = False

        return logger_console

    def setup_logger_file(self) -> logging.Logger:
        """Create a file-only logger that writes to ``QC/app.log``.

        Builds a :class:`logging.Logger` named ``[QC]`` configured at
        ``DEBUG`` with a :class:`logging.FileHandler` and propagation
        disabled so log records do not also leak to the root logger.
        Emits a session-start banner once the handler is attached.

        Returns:
            Configured :class:`logging.Logger` instance with a single
            file handler attached.

        Raises:
            OSError: If the log file path is not writable.

        Example:
            Use the file logger to record a custom event::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.logger_file.info("[QC] Custom test run")  # doctest: +SKIP

        Note:
            This logger is intended for events that should be archived
            but not echoed to the console, such as raw payload dumps.
        """
        # Define the log file name
        log_file = "app.log"

        # Get the script directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(script_dir, log_file)

        # Create a logger for file
        logger_file = logging.getLogger("[QC]")
        logger_file.setLevel(logging.DEBUG)  # Set the threshold level to DEBUG

        # Create a file handler
        file_handler = logging.FileHandler(log_path, mode="a")
        file_handler.setLevel(logging.DEBUG)

        # Create a formatter and set it for the file handler
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(formatter)

        # Add the file handler to the logger
        logger_file.addHandler(file_handler)

        # Prevent propagation to avoid logging to both handlers
        logger_file.propagate = False

        # Log the start of a new logging session
        logger_file.info("---------- New logging session started. ----------")
        return logger_file

    def indirect(self, i: str) -> bool:
        """Dispatch a QC command to the appropriate sensor test or special command.

        Looks up the command code in :obj:`QC.qcSensorList` to find the
        matching sensor configuration (e.g., :obj:`QC.sht31`,
        :obj:`QC.dust`, :obj:`QC.svantek`), instantiates the wrapper
        via :meth:`.sensorClass`, runs four read iterations via the
        wrapper's ``getSensorReading`` method, then validates the
        result with :meth:`.sensorDataValidation`. Pass/fail is also
        POSTed to the Oizom socket API via :meth:`.sendDataToSocket`
        when an operator supplies a sensor ID.

        Args:
            i: Command string (e.g., ``"d"`` for SHT31, ``"h"`` for
                dust). May contain a space-separated sensor ID suffix
                that is stored on :obj:`QC.i` for downstream payloads.

        Returns:
            ``True`` if the sensor produced valid data, ``False``
            otherwise. Special command branches (e.g., ``command_*``
            methods) may return their own values.

        Raises:
            AttributeError: If the resolved method on the sensor wrapper
                is missing.
            Exception: Any error raised by the underlying sensor driver
                is propagated; callers typically log and continue.

        Example:
            Run a single SHT31 read cycle::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.indirect("d")  # doctest: +SKIP

        Note:
            Requires real hardware; reads block until the sensor either
            replies or times out (see :obj:`QC.TIMEOUT`).
        """
        i = i.split(" ")
        self.i = i
        if i[0] not in self.qcSensorList.keys():
            method_name = "command_" + str(i[0])
            method = getattr(self, method_name, lambda: "Invalid")
            return method()
        sensor_name = self.qcSensorList[
            i[0]
        ]  # i i[0] command will store and than it will match with qcSensorList
        method = getattr(
            self, sensor_name, lambda: "Invalid"
        )  # Here we will get configuration of sensor
        self.sensor = self.sensorClass(method["class"])
        init = method["init"]
        sensor = method["sensor"]
        rprint(f"[HydroQC] Config: {init}")
        self.logger_file.info(f"[HydroQC] Config: {init}")
        if sensor == "gps" or sensor == "system":
            self.sensor.initialize(init)

        elif sensor == "wind2" or sensor == "rain2":
            self.sensor.initialize(self.samd_serial, init[0])

        elif sensor == "svantek":
            self.sensor.initialize(init)

        elif sensor == "noise2" or sensor == "helium_noise":
            self.sensor.initialize(init[0])

        else:
            self.sensor.initialize(init, self.init_value)

        for _ in track(range(4), description="[green]Sensor QC progress"):
            for _read in range(1):
                self.sensor.getSensorReading()
        time.sleep(3)
        sensorExists, value = self.sensorDataValidation(sensor)
        sensorId = input("Please Enter Sensor Id:> ")
        if len(sensorId) > 0:
            payload = {
                "SensorId": sensorId,
                "CommandId": self.i,
                "SensorData": str(value),
            }
            print(f"[QC] SENDING PAYLOAD: {payload}")
            self.sendDataToSocket(payload)
        if sensorExists:
            rprint("[green] Sensor data: ", value)
            self.logger_file.info(f"[green] Sensor data: {value}")
        elif not sensorExists:
            rprint(f"[red][ERR] {sensor} Sensor not Found/not Working")
            self.logger_file.error(f"[red][ERR] {sensor} Sensor not Found/not Working")
        self.logger_console.info(f"Sensor Exist: {sensorExists}")
        return sensorExists

    def sensorClass(self, sensor: str):
        """Return a fresh wrapper instance for the given sensor class name.

        Acts as a small factory that maps internal class identifiers
        (the ``"class"`` key in sensor config dicts such as
        :obj:`QC.sht31`) to concrete OzWrapper or driver classes such
        as :class:`OzWrapper.OzTemp.OzTemp` or
        :class:`drivers.SVANTEK.SVANTEK`.

        Args:
            sensor: Internal class identifier string (e.g.,
                ``"oztemp"``, ``"ozdust"``, ``"svantek"``,
                ``"oznoisev2"``).

        Returns:
            An uninitialised OzWrapper or driver instance, or ``None``
            if the identifier is not recognised.

        Raises:
            TypeError: If the underlying wrapper constructor rejects
                the default arguments.

        Example:
            Obtain a fresh SHT31 wrapper::

                >>> qc = QC()  # doctest: +SKIP
                >>> isinstance(qc.sensorClass("oztemp"), object)  # doctest: +SKIP
                True

        Note:
            The returned object still needs to be initialised by the
            caller (typically through ``initialize(...)``) before any
            sensor reads are valid.
        """
        if sensor == "samd":
            return OzSamd()
        if sensor == "oztemp":
            return OzTemp()
        if sensor == "ozbatt":
            return OzBattery()
        if sensor == "ozgps":
            return OzGPS()
        if sensor == "ozdust":
            return OzDust()
        if sensor == "ozogs":
            return OzOGS()
        if sensor == "ozuvlight":
            return OzUVLight()
        if sensor == "ozflood":
            return OzFlood()
        if sensor == "ozsystem":
            return OzSystem()
        if sensor == "ozco2":
            return OzCO2()
        if sensor == "oznoise":
            return OzNoise()
        if sensor == "ozwind":
            return OzWind()
        if sensor == "ozrain":
            return OzRain()
        if sensor == "svantek":
            return SVANTEK()
        if sensor == "wind2":
            return Wind()
        if sensor == "rain2":
            return Rain()
        if sensor == "noise2":
            return Noise()
        if sensor == "wind3":
            return OzWindV2()
        if sensor == "ozsoil":
            return OzSoil()
        if sensor == "oizomnoisev2":
            return OizomNoiseV2()
        if sensor == "ozlightning":
            return OzLightning()
        if sensor == "ozosp":
            return OzOSP()

    def sensorDataValidation(self, sensorType: str) -> tuple[bool, dict]:
        """Validate sensor output and update the sensor status list.

        Calls ``putSensorValue()`` on the active sensor stored in
        :obj:`QC.sensor`, then applies sensor-specific rules to
        determine whether the sensor is actually working. The noise
        sensor is treated as failed when all channels report the
        idle pattern ``{"leq": 0.0, "lmax": 0.0, "lmin": 255.0}``; the
        rain sensor is treated as failed when the bucket sentinel
        values (``0`` or ``0.011``) are observed; wind requires both
        ``wd`` and ``ws`` to be non-zero. Other sensors simply require
        a non-empty dict.

        Args:
            sensorType: Short sensor name matching the keys of
                :obj:`QC.sensorStatusList` (e.g., ``"sht"``, ``"dust"``,
                ``"noise"``).

        Returns:
            Tuple ``(sensor_exists, value_dict)`` where ``sensor_exists``
            is ``True`` when the sensor produced valid, non-trivial
            data. The dictionary is also forwarded to upstream
            payloads by :meth:`.indirect`.

        Raises:
            KeyError: If ``sensorType`` is not present in
                :obj:`QC.sensorStatusList`.

        Example:
            Validate the active SHT reading after :meth:`.indirect`::

                >>> qc = QC()  # doctest: +SKIP
                >>> ok, data = qc.sensorDataValidation("sht")  # doctest: +SKIP

        Note:
            Has side effects: mutates :obj:`QC.sensorStatusList` so the
            final QC table reflects the latest result.
        """
        data = {}
        sensorExists = False
        sensor = sensorType
        value = self.sensor.putSensorValue(data)
        print(f"[QC] Sensor Value: {value}")
        if sensor == "noise":
            self.sensorStatusList["noise"] = (
                0
                if value == {"leq": 0.0, "lmax": 0.0, "lmin": 255.0} or value == {}
                else 1
            )
            sensorExists = (
                False if value == {"leq": 0.0, "lmax": 0.0, "lmin": 255.0} else True
            )
        elif sensor == "rain":
            self.sensorStatusList["rain"] = (
                0
                if value == {"rain": 0} or value == {"rain": 0.011} or value == {}
                else 1
            )
            sensorExists = (
                False if value == {"rain": 0} or value == {"rain": 0.011} else True
            )
        elif sensor == "wind":
            self.sensorStatusList["wind"] = (
                0 if value == {"wd": 0, "ws": 0} or value == {} else 1
            )
            sensorExists = False if value == {"wd": 0, "ws": 0} else True
        else:
            self.sensorStatusList[sensor] = 0 if value == {} else 1
            sensorExists = False if value == {} else True
        return sensorExists, value

    def command_i(self) -> None:
        """Cycle through all RGB LED status patterns for visual verification.

        Iterates over :obj:`QC.ledStatus`, pushing each index into the
        :obj:`QC.network_status` queue so the daemon LED thread updates
        the on-board NeoPixel (:class:`OzWrapper.OzRGB`) accordingly.
        Operators visually confirm that every status colour is
        reachable.

        Returns:
            None. Output is visual: the on-board RGB cycles through
            each pre-defined pattern, one per second.

        Raises:
            queue.Full: If the LED queue is unexpectedly full.

        Example:
            Run the LED self-test::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_i()  # doctest: +SKIP

        Note:
            Hardware-only; relies on the LED thread spawned in
            :meth:`.__init__`.
        """
        for i in range(len(self.ledStatus)):
            self.network_status.queue[0] = i
            rprint("LED Status -> ", self.ledStatus[i])
            self.logger_file.info(f"LED Status -> {self.ledStatus[i]}")
            time.sleep(1)

    def readFAN(self, fan_delay: int) -> bool:
        """Sample fan tachometer signals and determine fan health.

        Polls the two fan-tach pins on :obj:`QC.MCP`
        (:obj:`MCP230XX.PIN_A` and :obj:`MCP230XX.PIN_B`) continuously
        for ``fan_delay`` seconds, then computes the duty-cycle for
        each. A pin stuck at ``0`` or ``1`` indicates the fan is
        stalled and the method writes ``0`` to the fan-status output;
        otherwise it writes ``1`` to flag a healthy fan.

        Args:
            fan_delay: Duration in seconds to sample the fan GPIO pins.

        Returns:
            ``True`` if the fan is spinning normally, ``False`` if
            either tach line is stuck.

        Raises:
            AttributeError: If :obj:`QC.MCP` was never instantiated.

        Example:
            Sample for five seconds after powering the fan::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_l()  # spins up fan and calls readFAN  # doctest: +SKIP

        Note:
            Blocks the calling thread for ``fan_delay`` seconds while
            it polls the GPIO lines.
        """
        status1, status2 = [], []
        time_prev = time.monotonic()
        while fan_delay > (time.monotonic() - time_prev):
            status1.append(self.MCP.input(self.MCP.PIN_A))
            status2.append(self.MCP.input(self.MCP.PIN_B))
            time.sleep(0.01)  # avoid a tight I2C busy-poll
        if not status1 or not status2:
            self.logger_console.info("[FAN] no samples read; treating as fault")
            self.MCP.digitalWrite(self.MCP.FAN_STATUS, 0)
            return False
        err1 = sum(status1) / len(status1)
        err2 = sum(status2) / len(status2)
        self.logger_console.info(f"[FAN] {err1}, {err2}")
        if err1 == 0 or err1 == 1 or err2 == 0 or err2 == 1:
            self.MCP.digitalWrite(self.MCP.FAN_STATUS, 0)
            return False
        self.MCP.digitalWrite(self.MCP.FAN_STATUS, 1)
        return True

    def command_l(self) -> bool:
        """Test fan operation by powering it on, checking tachometer, then off.

        Instantiates a fresh :class:`drivers.MCP230XX.MCP230XX`, resets
        outputs to defaults, drives the fan high, samples tach lines
        via :meth:`.readFAN` for five seconds, then drives the fan
        low. Operator must visually confirm fan spin-down.

        Returns:
            ``True`` once the fan power cycle completes regardless of
            tach health. Tach status is logged separately.

        Raises:
            OSError: If the MCP I2C device fails to acknowledge.

        Example:
            Run the fan test::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_l()  # doctest: +SKIP

        Note:
            Hardware-only; expects ``MCP_ID`` env var to point at the
            correct MCP23017 address.
        """
        self.MCP = MCP230XX(devicenumber=int(os.getenv("MCP_ID", 6)))
        self.MCP.resetDefault()
        self.MCP.FAN_HIGH()
        fanStatus = self.readFAN(5)
        rprint("Fan powered ON")
        self.logger_file.info("Fan Power ON")
        rprint(f"Status: {fanStatus}")
        self.logger_file.info(f"Status: {fanStatus}")
        time.sleep(2)
        self.MCP.FAN_LOW()
        rprint("Fan powered OFF")
        self.logger_file.info("Fan powered OFF")
        return True

    def command_m(self) -> dict | tuple:
        """Query the Network Manager for Ethernet, GSM, and WiFi status.

        Discovers the gateway IP via :meth:`.findBridgeIP`, builds the
        full status endpoint URL, performs a GET, and prints the
        ``eth``, ``gsm``, and ``wifi`` reachability flags to the
        console.

        Returns:
            Network status :class:`dict` on success, or the tuple
            ``({}, 404)`` on failure.

        Raises:
            requests.RequestException: Caught internally and logged;
                the function returns a sentinel tuple instead.

        Example:
            Print the current connectivity status::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_m()  # doctest: +SKIP

        Note:
            Requires the Network Manager microservice to be reachable
            via the bridge gateway.
        """
        try:
            ip_address = self.findBridgeIP()
            url = ip_address + self.status_api
            self.response = requests.get(url)
            self.logger_console.info(f"URL: {url}")
            status = json.loads(json.dumps(self.response.json()))
            if self.response.status_code == 200:
                self.logger_console.info(f"URL: {url}")
                rprint("Ethernet -> ", status["eth"]["internet"])
                self.logger_file.info(f"Ethernet -> {status['eth']['internet']}")
                rprint("GSM -> ", status["gsm"]["internet"])
                self.logger_file.info(f"GSM -> {status['gsm']['internet']}")
                rprint("WiFi -> ", status["wifi"]["internet"])
                self.logger_file.info(f"WiFi -> {status['wifi']['internet']}")
                return status
            return ({}, self.response.status_code)
        except Exception as e:
            rprint(e)
            self.logger_file.exception(e)
            return ({}, 404)

    def command_qo(self) -> int | tuple:
        """Upload SAMD firmware binary to the Network Manager service.

        Opens the SAMD firmware blob at
        ``/usr/src/app/QC/Samdfirmware_v3.bin``, builds the multipart
        upload, and POSTs it to ``<bridge>/samd/firmware/update``.

        Returns:
            HTTP status code (:class:`int`) on success, or the tuple
            ``({}, 404)`` on failure.

        Raises:
            FileNotFoundError: If the firmware binary is missing.
            requests.RequestException: Caught internally and logged.

        Example:
            Flash new SAMD firmware before a re-test::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qo()  # doctest: +SKIP

        Note:
            After a successful upload the device requires roughly 30
            seconds to reflash before further QC steps will succeed.
        """
        # print("[QC] pass - no code")
        try:
            ip_address = self.findBridgeIP()
            url = ip_address + self.samd_api
            self.logger_console.info(url)
            binary_file_path = "/usr/src/app/QC/Samdfirmware_v3.bin"
            self.logger_console.info(
                f"QC file size: {os.path.getsize(binary_file_path)}"
            )
            try:
                with open(binary_file_path, "rb") as fw:
                    files = {"firmware_bin": fw}
                    self.response = requests.post(
                        url, files=files, headers=self.headers
                    )
            except Exception as e:
                rprint(e)
                self.logger_file.exception(e)
                return None
            if self.response.ok:
                self.logger_console.info(f"{self.response.text}")
            else:
                self.logger_console.info("Please Upload again! ")
            if self.response.status_code == 200:
                rprint(self.response.status_code)
                self.logger_file.info(self.response.status_code)
                rprint(self.response.content)
                self.logger_file.info(self.response.content)
                rprint(self.response.headers)
                self.logger_file.info(self.response.headers)
                rprint("[yellow] Please wait for 30 seconds")
                self.logger_file.info("[yellow] Please wait for 30 seconds")
            return self.response.status_code
        except Exception as e:
            rprint(e)
            self.logger_file.exception(e)
            return ({}, 404)

    def command_qj(self) -> int | tuple:
        """Trigger a WiFi network scan via the Network Manager.

        Sends a GET to ``<bridge>/network/wifi/scan`` and logs the
        response status, body, and headers for the operator.

        Returns:
            HTTP status code (:class:`int`) on success, or the tuple
            ``({}, 404)`` on failure.

        Raises:
            requests.RequestException: Caught internally and logged;
                the function returns a sentinel tuple instead.

        Example:
            Run a WiFi sweep::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qj()  # doctest: +SKIP

        Note:
            Network Manager must be online for the call to succeed.
        """
        try:
            ip_address = self.findBridgeIP()
            url = ip_address + self.scan_wifi_api
            self.logger_console.info(url)
            r = requests.get(url)
            rprint(r.status_code)
            self.logger_file.info(r.status_code)
            rprint(r.content)
            self.logger_file.info(r.content)
            rprint(r.headers)
            self.logger_file.info(r.headers)
            rprint("[yellow] WiFi Scanning")
            self.logger_file.info("[yellow] WiFi Scanning")
            return r.status_code
        except Exception as e:
            rprint(e)
            self.logger_file.exception(e)
            return ({}, 404)

    def command_qh(self) -> int | tuple:
        """Create a WiFi hotspot (access point) on the device.

        Sends a GET to ``<bridge>/network/wifi/createap`` so the
        Network Manager spins up a SoftAP for field service.

        Returns:
            HTTP status code (:class:`int`) on success, or the tuple
            ``({}, 404)`` on failure.

        Raises:
            requests.RequestException: Caught internally and logged.

        Example:
            Generate an AP for a field tablet to join::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qh()  # doctest: +SKIP

        Note:
            Will tear down the active WiFi-client connection.
        """
        try:
            ip_address = self.findBridgeIP()
            url = ip_address + self.generate_hotspot_api
            self.logger_console.info(url)
            r = requests.get(url)
            rprint(r.status_code)
            self.logger_file.info(r.status_code)
            rprint(r.content)
            self.logger_file.info(r.content)
            rprint(r.headers)
            self.logger_file.info(r.headers)
            rprint("[yellow] AP generated")
            self.logger_file.info("[yellow] AP generated")
            return r.status_code
        except Exception as e:
            rprint(e)
            self.logger_file.exception(e)
            return ({}, 404)

    def command_qi(self) -> int | tuple:
        """Connect the device to the Oizom WiFi network.

        Posts the configured SSID (:obj:`QC.SSID`) and password
        (:obj:`QC.password`) to ``<bridge>/network/wifi/connect``.

        Returns:
            HTTP status code (:class:`int`) on success, or the tuple
            ``({}, 404)`` on failure.

        Raises:
            requests.RequestException: Caught internally and logged.

        Example:
            Reconnect to the lab WiFi after a hotspot session::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qi()  # doctest: +SKIP

        Note:
            Hard-coded for the ``OIZOM`` SSID; modify the class
            attributes for site-specific networks.
        """
        try:
            ip_address = self.findBridgeIP()
            url = ip_address + self.connect_Oizom_api
            self.logger_console.info(url)
            connection = {"ssid": self.SSID, "password": self.password}
            r = requests.post(url, json=connection)
            rprint(r.status_code)
            self.logger_file.info(r.status_code)
            rprint(r.content)
            self.logger_file.info(r.content)
            rprint(r.headers)
            self.logger_file.info(r.headers)
            rprint(r.text)
            self.logger_file.info(r.text)
            rprint("[yellow] Connecting to OIZOM WiFI")
            self.logger_file.info("[yellow] Connecting to OIZOM WiFI")
            return r.status_code
        except Exception as e:
            rprint(e)
            self.logger_file.exception(e)
            return ({}, 404)

    def findBridgeIP(self) -> str:
        """Discover the Docker bridge gateway IP and return the Network Manager URL.

        Runs ``getent hosts $HOSTNAME | awk '{print $1}'`` to obtain
        the container IP, replaces the last octet with ``1`` to find
        the bridge gateway, and wraps the result in
        ``http://<ip>:8084``.

        Returns:
            Full URL string for the Network Manager service (e.g.,
            ``"http://172.17.0.1:8084"``). Falls back to
            ``"172.17.0.1"`` if ``getent`` errors out.

        Raises:
            subprocess.SubprocessError: Caught internally and logged.

        Example:
            Resolve the bridge URL from inside a container::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.findBridgeIP()  # doctest: +SKIP

        Note:
            Assumes a standard ``/24`` Docker bridge where the gateway
            ends in ``.1``.
        """
        command = "getent hosts $HOSTNAME | awk '{print $1}'"
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True)
            rprint("[yellow] Output:")
            self.logger_file.info("[yellow] Output:")
            rprint(result.stdout)
            self.logger_file.info(result.stdout)
            if result.stderr:
                rprint("[red] Error:")
                self.logger_file.info("[red] Error:")
                rprint(result.stderr)
                self.logger_file.info(result.stderr)
                return "http://172.17.0.1:8084"
            ip_address = result.stdout
            ip_address = ip_address.replace("\n", "")
            ip_address_parts = ip_address.split(".")
            ip_address_parts[-1] = "1"
            modified_ip = ".".join(ip_address_parts)
            url = "http://" + modified_ip + ":8084"
            return url
        except Exception as e:
            rprint(f"[red] An error occurred: {e}")
            self.logger_file.exception(f"[red] An error occurred: {e}")
            return "172.17.0.1"  # default bridge IP

    def command_qg(self) -> int:
        """Check the hardware real-time clock (RTC) via kernel ``dmesg`` logs.

        Greps the kernel ring buffer for RTC-related messages. The
        presence of ``setting system clock`` is taken as a healthy
        RTC; ``Power loss detected`` indicates the backup battery has
        failed. The result is also persisted in
        :obj:`QC.sensorStatusList` under the ``"rtc"`` key.

        Returns:
            ``1`` if the RTC is working, ``0`` otherwise.

        Raises:
            subprocess.SubprocessError: Caught internally and logged.

        Example:
            Confirm RTC health before kicking off HOLO allocation::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qg()  # doctest: +SKIP

        Note:
            Requires read access to ``dmesg``; some kernels restrict
            this to root or processes with ``CAP_SYSLOG``.
        """
        flag = 0
        try:
            command = "dmesg | grep rtc"
            result = subprocess.run(command, shell=True, capture_output=True, text=True)
            rprint("[yellow] Output:")
            self.logger_file.info("[yellow] Output:")
            rprint(result.stdout)
            self.logger_file.info(result.stdout)
            if "setting system clock" in result.stdout:
                flag = 1
                rprint("[green] RTC WORKING")
                self.logger_file.info("[green] RTC WORKING")
                self.sensorStatusList["rtc"] = 1
            if "Power loss detected" in result.stdout:
                flag = 0
                rprint("[red] RTC NOT-WORKING")
                self.logger_file.info("[red] RTC NOT-WORKING")
                self.sensorStatusList["rtc"] = 0
            if result.stderr:
                rprint("[red] Error:")
                self.logger_file.info("[red] Error:")
                rprint(result.stderr)
                self.logger_file.info(result.stderr)
            return flag
        except Exception as e:
            rprint(f"[red] An error occurred: {e}")
            self.logger_file.exception(f"[red] An error occurred: {e}")
            return flag

    def command_qf(self) -> bool:
        """Enable the QC bypass flag to skip sensor validation gates.

        Sets :obj:`QC.qcBypass` to ``True`` so that the HOLO/MB
        allocation paths in :meth:`.comm_c` and :meth:`.comm_hc`
        proceed even when one or more sensors have failed.

        Returns:
            The new value of :obj:`QC.qcBypass` (always ``True``).

        Raises:
            None.

        Example:
            Bypass validation during an investigative run::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qf()  # doctest: +SKIP
                True

        Note:
            Intended for engineering use only; do not bypass on
            production boards.
        """
        self.qcBypass = True
        return self.qcBypass

    def command_exe(self) -> None:
        """Open an interactive shell prompt for executing arbitrary commands.

        Enters a read-eval-print loop that runs each input through
        :func:`subprocess.run` with ``shell=True``, then echoes the
        ``stdout`` and ``stderr`` to the QC console and file logs.
        Exits when the operator types ``exit``.

        Returns:
            None. Loop terminates only on the ``exit`` keyword.

        Raises:
            subprocess.SubprocessError: Caught internally and logged.

        Example:
            Open the shell and inspect ``ifconfig``::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_exe()  # type 'ifconfig' then 'exit'  # doctest: +SKIP

        Note:
            Runs with the privileges of the QC process; treat input
            as trusted.
        """
        while True:
            command = input("Enter a command (or 'exit' to quit): ")
            try:
                result = subprocess.run(
                    command, shell=True, capture_output=True, text=True
                )
                rprint("[yellow] Output:")
                self.logger_file.info("[yellow] Output:")
                rprint(result.stdout)
                self.logger_file.info(result.stdout)
                if result.stderr:
                    rprint("[red] Error:")
                    self.logger_file.info("[red] Error:")
                    rprint(result.stderr)
                    self.logger_file.info(result.stderr)
            except Exception as e:
                rprint(f"[red] An error occurred: {e}")
                self.logger_file.exception(f"[red] An error occurred: {e}")
            if command.lower() == "exit":
                rprint("[yellow] --- END ---")
                self.logger_file.info("[yellow] --- END ---")
                break

    def command_help(self) -> None:
        """Print the available command-line argument mappings.

        Writes :obj:`QC.argsParse` to the console logger so the
        operator can review every ``--flag`` understood by the harness.

        Returns:
            None. Output is purely informational.

        Raises:
            None.

        Example:
            Display the full ``--help`` table::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_help()  # doctest: +SKIP

        Note:
            For human-readable descriptions instead of raw flag-to-code
            mappings, see :obj:`QC.argsCommand`.
        """
        self.logger_console.info(self.argsParse)
        # print(self.argsParse)

    def command_ql(self) -> bool:
        """Trigger a full system reboot via the SAMD watchdog.

        Initialises the SAMD wrapper (:class:`OzWrapper.OzSamd.OzSamd`)
        with :obj:`QC.samdWatchdog`, then invokes the wrapper's
        ``rebootSystem`` to issue a hardware-level reset.

        Returns:
            ``True`` immediately after the reboot signal is dispatched
            (the device usually power-cycles before the caller sees
            the return value).

        Raises:
            AttributeError: If :obj:`QC.samd` was not pre-instantiated.

        Example:
            Reboot a wedged device::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_ql()  # doctest: +SKIP

        Note:
            Hardware-only; will sever any active SSH/console session.
        """
        self.samd.initialize(self.samdWatchdog["samd"], self.init_value)
        self.samd.rebootSystem()
        rprint("Rebooting System.. Signing Off.. Bye Bye!!!")
        self.logger_file.info("Rebooting System.. Signing Off.. Bye Bye!!!")
        return True

    def command_qn(self) -> bool:
        """Reboot the 3.3 V power rail via the MCP I/O expander.

        Spawns a fresh :class:`drivers.MCP230XX.MCP230XX` instance,
        resets its outputs, then calls ``power_3v3_rst()`` to cycle
        the 3.3 V rail that feeds the sensor I2C peripherals.

        Returns:
            ``True`` once the reset completes.

        Raises:
            OSError: If the MCP I2C device does not respond.

        Example:
            Recover from a wedged I2C bus::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qn()  # doctest: +SKIP

        Note:
            Hardware-only; toggling 3.3 V also resets the SHT, BME,
            and battery monitor.
        """
        self.MCP = MCP230XX(devicenumber=int(os.getenv("MCP_ID", 6)))
        self.MCP.resetDefault()
        rprint("Rebooting 3.3V")
        self.logger_file.info("Rebooting 3.3V")
        self.MCP.power_3v3_rst()
        rprint("Reboot completed")
        self.logger_file.info("Reboot completed")
        return True

    def command_qm(self) -> bool:
        """Reboot the 5 V power rail via the MCP I/O expander.

        Spawns a fresh :class:`drivers.MCP230XX.MCP230XX`, resets
        outputs to defaults, then calls ``power_5v_rst()`` to cycle
        the 5 V rail that powers the dust, OGS, and Cubic sensors.

        Returns:
            ``True`` once the reset completes.

        Raises:
            OSError: If the MCP I2C device does not respond.

        Example:
            Recover from a stuck Cubic dust sensor::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qm()  # doctest: +SKIP

        Note:
            Hardware-only; allow several seconds for the 5 V rail to
            stabilise before retesting the sensors.
        """
        self.MCP = MCP230XX(devicenumber=int(os.getenv("MCP_ID", 6)))
        self.MCP.resetDefault()
        rprint("Rebooting 5V")
        self.logger_file.info("Rebooting 5V")
        self.MCP.power_5v_rst()
        rprint("Reboot completed")
        self.logger_file.info("Reboot completed")
        return True

    def command_q(self) -> None:
        """Display the full QC command-list table in the console.

        Renders :obj:`QC.qcCommands` through :meth:`.generate_table` so
        the operator can review every supported single-letter command.

        Returns:
            None. Output is a Rich table printed to stdout.

        Raises:
            None.

        Example:
            Show the command list at startup::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_q()  # doctest: +SKIP

        Note:
            For diagnostic/hidden commands see :meth:`.command_jj`.
        """
        rprint("[yellow] QC COMMAND-LIST TABLE")
        self.logger_file.info("[yellow] QC COMMAND-LIST TABLE")
        self.generate_table(self.qcCommands)

    def command_s(self) -> bool:
        """Test relay outputs by toggling each channel on and off.

        Drives Raspberry Pi GPIO pins 26 (``output1``) and 16
        (``output2``) high for one second, then low, while logging
        the transitions for the operator to verify with a multimeter.

        Returns:
            ``True`` once all configured relays have been cycled.

        Raises:
            RuntimeError: If GPIO setup fails.

        Example:
            Cycle the relays during HOLO QC::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_s()  # doctest: +SKIP

        Note:
            Hardware-only; ensure that downstream loads can tolerate
            being switched.
        """
        parts = {
            "output1": 26,
            "output2": 16,
        }  # defines pin numbers of the output on relays
        for key in parts:
            gpio.setup(parts[key], gpio.OUT)
            gpio.set(parts[key], gpio.HIGH)
            rprint("Enabling ", key)
            self.logger_file.info(f"Enabling {key}")
            time.sleep(1)
            gpio.set(parts[key], gpio.LOW)
            rprint("Disabling ", key)
            self.logger_file.info(f"Disabling {key}")
            time.sleep(1)
        return True

    def command_y(self) -> bool:
        """Test the TM1637 six-digit display and unit-indicator LEDs.

        Drives the unit-indicator LEDs (MCP pins 12-15) on, scrolls
        the message ``"888888"`` on the
        :class:`drivers.TM1637.TM1637.TM1637Decimal` display, then
        clears the display and turns the indicator LEDs off.

        Returns:
            ``True`` once the display sequence completes.

        Raises:
            OSError: If the TM1637 CLK/DIO pins cannot be claimed.

        Example:
            Run the display self-test::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_y()  # doctest: +SKIP

        Note:
            Hardware-only; expects CLK on GPIO 21 and DIO on GPIO 20.
        """
        CLK, DIO = 21, 20
        self.MCP = MCP230XX(devicenumber=int(os.getenv("MCP_ID", 6)))
        self.tm1637 = TM1637Decimal(clk=CLK, dio=DIO)
        unit_pin = [12, 13, 14, 15]
        for pin in unit_pin:
            self.MCP.pinMode(pin, "output")
            self.MCP.digitalWrite(pin, 0)
            time.sleep(0.1)
            self.MCP.digitalWrite(pin, 1)
        rprint("[DISPLAY] Running...")
        self.logger_file.info("[DISPLAY] Running...")
        message = "888888"
        rprint("[Display] ", message)
        self.logger_file.info(f"[Display] {message}")
        self.tm1637.write(self.clearDisplay)
        time.sleep(0.5)
        self.tm1637.scroll(message)
        self.tm1637.write(self.clearDisplay)
        for pin in unit_pin:
            self.MCP.digitalWrite(pin, 0)
            time.sleep(0.1)
        return True

    def command_hy(self) -> bool:
        """Test the Helium TM1638 display with segment and LED patterns.

        Instantiates a :class:`drivers.TM1638.TM1638.TM1638`, walks
        four indicator LEDs on, scrolls the segment-checker pattern
        ``"8.8.8.8.8.8."`` and clears the display.

        Returns:
            ``True`` once the test sequence completes.

        Raises:
            OSError: If the TM1638 STB/CLK/DIO pins cannot be claimed.

        Example:
            Run the Helium display self-test::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_hy()  # doctest: +SKIP

        Note:
            Hardware-only; expects STB=GPIO3, CLK=GPIO21, DIO=GPIO20.
        """
        CLK = 21
        DIO = 20
        STB = 3
        display = TM1638(stb=STB, clk=CLK, dio=DIO, brightness=7)
        display.clear_leds()
        unit_pin = [0, 1, 2, 3]
        for pin in unit_pin:
            display.update_led(pin)
            time.sleep(0.5)
        rprint("[DISPLAY] Running...")
        self.logger_file.info("[DISPLAY] Running...")
        message = "8.8.8.8.8.8."
        rprint("[Display] ", message)
        self.logger_file.info(f"[Display] {message}")
        display.show(message)
        time.sleep(5)
        display.write(self.clearDisplay)
        time.sleep(1)
        display.clear_leds()
        return True

    def command_qk(self) -> bool:
        """Display a custom or default message on the TM1637 display.

        Prompts the operator for an optional message. If they enter
        ``Y``, the input string is padded or truncated to fit the
        six-digit display; ``N`` falls back to ``"888888"``. The
        message is scrolled and cleared.

        Returns:
            ``True`` once the message has been displayed.

        Raises:
            OSError: If the TM1637 pins are unavailable.

        Example:
            Run a quick smoke test of the display::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qk()  # answer 'N' at the prompt  # doctest: +SKIP

        Note:
            Hardware-only; uses the same TM1637 wiring as
            :meth:`.command_y`.
        """
        CLK, DIO = 21, 20
        self.tm1637 = TM1637Decimal(clk=CLK, dio=DIO)
        rprint("[DISPLAY] Running...")
        self.logger_file.info("[DISPLAY] Running...")
        message = ""
        YorN = input("ANY MESSAGE .?   Y or N ")
        if YorN == ("Y"):
            message = input("Enter message: ")
            mLenght = len(message.replace(".", ""))
            if mLenght > 6:
                message = "8-8-8-"
            if mLenght < 6:
                message = " " * (6 - mLenght) + message
            rprint(f"[display] message {message}")
            self.logger_file.info(f"[display] message {message}")
        elif YorN == ("N"):
            message = "888888"
        rprint("[Display] ", message)
        self.logger_file.info(f"[Display] {message}")
        self.tm1637.write(self.clearDisplay)
        time.sleep(0.5)
        self.tm1637.scroll(message)
        self.tm1637.write(self.clearDisplay)
        return True

    def command_cd(self) -> bool:
        """Run the dust sensor calibration process using the DustCal config.

        Loads the static configuration file at
        ``/usr/src/app/OzWrapper/OzDustCal/dustcal.config.json``,
        instantiates :class:`OzWrapper.OzDustCal.OzDustCal.OzDustCal`,
        executes two read cycles, then dumps the resulting calibration
        values via ``putSensorValue``.

        Returns:
            ``True`` once the calibration cycle completes.

        Raises:
            FileNotFoundError: If the calibration JSON is missing.
            json.JSONDecodeError: If the calibration JSON is malformed.

        Example:
            Calibrate the dust sensor in the calibration chamber::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_cd()  # doctest: +SKIP

        Note:
            Hardware-only; assumes the dust sensor has been warmed up.
        """
        dustCal = OzDustCal()
        dirname = os.path.dirname(__file__)
        file_name = os.path.join(
            dirname, "/usr/src/app/OzWrapper/OzDustCal/dustcal.config.json"
        )
        with open(file_name) as dustCalConfigFile:
            data = dustCalConfigFile.read()
        dustCalConfig = json.loads(data)
        print(dustCalConfig)
        dustCal = OzDustCal()
        dustCal.initialize(dustCalConfig["dustCal"], {})
        print("[dustCal] Reading start \n\n")
        for i in range(2):
            dustCal.getSensorReading()
        data = {}
        print("[dustCal] PutSensor value start \n\n")
        print(dustCal.putSensorValue(data))
        return True

    def command_z(self) -> bool:
        """Test the siren (LED + buzzer) via the MCP I/O expander.

        Drives the LED (MCP pin 10) high for three seconds, then off,
        then the buzzer (MCP pin 11) high for three seconds, then off.

        Returns:
            ``True`` once both outputs have been exercised.

        Raises:
            OSError: If the MCP I2C device does not respond.

        Example:
            Test the siren on a HOLO assembly::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_z()  # doctest: +SKIP

        Note:
            Hardware-only; expect audible noise during the test.
        """
        self.MCP = MCP230XX(devicenumber=int(os.getenv("MCP_ID", 6)))
        parts = {"LED": 10, "Buzzer": 11}  # defines pin numbers of the output on relays
        for key in parts:
            self.MCP.pinMode(parts[key], "output")
            self.MCP.digitalWrite(parts[key], 1)
            rprint("Enabling ", key)
            self.logger_file.info(f"Enabling {key}")
            rprint("wait for 3 secs")
            self.logger_file.info("wait for 3 secs")
            time.sleep(3)
            self.MCP.digitalWrite(parts[key], 0)
            rprint("Disabling ", key)
            self.logger_file.info(f"Disabling {key}")
            time.sleep(1)
        return True

    def command_be(self) -> bool:
        """Test the NeoPixel beacon by cycling through representative AQI color values.

        Imports the helper utilities from :mod:`OzWrapper.OzRGB.OzRGB`,
        converts each AQI bucket value (10, 50, 100, 200, 300, 400)
        into its hex colour, then drives every pixel to that colour
        for two seconds.

        Returns:
            ``True`` once the colour sweep completes (also when the
            import fails, since the failure is logged but not
            re-raised).

        Raises:
            ImportError: Caught internally and logged; the function
                still returns a value.

        Example:
            Sweep the beacon during HOLO QC::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_be()  # doctest: +SKIP

        Note:
            Hardware-only; the NeoPixel ring must be wired and
            configured before the test.
        """
        try:
            from OzWrapper.OzRGB.OzRGB import get_color_from_value, hex_to_rgb, pixels
        except ImportError as e:
            rprint("[red] Beacon test dependencies missing or import failed.")
            self.logger_file.error(f"Beacon test import error: {e}")
            return False

        test_values = [10, 50, 100, 200, 300, 400]

        for value in test_values:
            color = get_color_from_value(value)
            rgb = hex_to_rgb(color)

            pixels[:] = [rgb] * len(pixels)
            pixels.show()

            print(f"Test value {value} -> {rgb}")
            time.sleep(2)

        return True

    def command_hz(self) -> bool:
        """Test the Helium board siren (GPIO-driven LED + buzzer).

        Drives GPIO 22 (LED) and GPIO 23 (buzzer) high for one second
        each, then low, using :mod:`drivers.gpio` instead of the MCP
        I/O expander used by the standard board.

        Returns:
            ``True`` once both outputs have been exercised.

        Raises:
            RuntimeError: If the GPIO setup fails.

        Example:
            Test the siren on a Helium HOLO assembly::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_hz()  # doctest: +SKIP

        Note:
            Hardware-only; expect audible noise during the test.
        """
        parts = {"LED": 22, "Buzzer": 23}
        for key in parts:
            gpio.setup(parts[key], gpio.OUT)
            gpio.set(parts[key], gpio.HIGH)
            rprint("Enabling ", key)
            self.logger_file.info(f"Enabling {key}")
            time.sleep(1)
            gpio.set(parts[key], gpio.LOW)
            rprint("Disabling ", key)
            self.logger_file.info(f"Disabling {key}")
            time.sleep(1)
        return True

    def command_qz(self) -> None:
        """Display the current sensor pass/fail status table.

        Renders :obj:`QC.sensorStatusList` through :meth:`.qcTable`
        so the operator can see which sensors have been validated so
        far in the current session.

        Returns:
            None. Output is a Rich table printed to stdout.

        Raises:
            None.

        Example:
            Review progress mid-session::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qz()  # doctest: +SKIP

        Note:
            Reflects accumulated state since the QC process started;
            re-running individual sensor tests will mutate it.
        """
        rprint("[yellow] QC STATUS TABLE")
        self.logger_file.info("[yellow] QC STATUS TABLE")
        self.qcTable(self.sensorStatusList)

    def command_qy(self) -> None:
        """Run all I2C sensor tests (SHT, BME, battery, light-UV) sequentially.

        Iterates over :obj:`QC.i2cCommands` and dispatches each entry
        through :meth:`.indirect`. Any exception raised by an
        individual test is caught, logged, and execution continues
        with the next sensor.

        Returns:
            None. Result is displayed at the end as a Rich status
            table via :meth:`.qcTable`.

        Raises:
            None.

        Example:
            Quickly verify every I2C device::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qy()  # doctest: +SKIP

        Note:
            Hardware-only; mutates :obj:`QC.sensorStatusList` for
            every sensor it touches.
        """
        for key, value in self.i2cCommands.items():
            rprint(str(key))
            self.logger_file.info(str(key))
            try:
                status = self.indirect(value)
                rprint(status)
                self.logger_file.info(status)
            except Exception as e:
                rprint(e)
                self.logger_file.exception(e)
        rprint("[yellow] QC STATUS TABLE")
        self.logger_file.info("[yellow] QC STATUS TABLE")
        self.qcTable(self.sensorStatusList)

    def command_qx(self) -> None:
        """Run all UART sensor tests (dust, OGS) sequentially.

        Iterates over :obj:`QC.uartComands` and dispatches each entry
        through :meth:`.indirect`. Any exception raised by an
        individual test is caught, logged, and execution continues.

        Returns:
            None. Result is displayed via :meth:`.qcTable` once the
            loop completes.

        Raises:
            None.

        Example:
            Verify every UART-attached sensor::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qx()  # doctest: +SKIP

        Note:
            Hardware-only; mutates :obj:`QC.sensorStatusList`.
        """
        for key, value in self.uartComands.items():
            rprint(str(key))
            self.logger_file.info(str(key))
            try:
                status = self.indirect(value)
                rprint(status)
                self.logger_file.info(status)
            except Exception as e:
                rprint(e)
                self.logger_file.exception(e)
        rprint("[yellow] QC STATUS TABLE")
        self.logger_file.info("[yellow] QC STATUS TABLE")
        self.qcTable(self.sensorStatusList)

    def command_qw(self) -> None:
        """Run all SAMD-connected sensor tests (noise, wind, rain) sequentially.

        Iterates over :obj:`QC.samdCommands` and dispatches each entry
        through :meth:`.indirect`, catching and logging any exception
        so the loop can complete.

        Returns:
            None. Result is displayed as a Rich status table via
            :meth:`.qcTable`.

        Raises:
            None.

        Example:
            Validate every SAMD-attached sensor::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qw()  # doctest: +SKIP

        Note:
            Hardware-only; depends on a working SAMD bridge on
            ``/dev/ttyACM0``.
        """
        for key, value in self.samdCommands.items():
            rprint(str(key))
            self.logger_file.info(str(key))
            try:
                status = self.indirect(value)
                rprint(status)
                self.logger_file.info(status)
            except Exception as e:
                rprint(e)
                self.logger_file.exception(e)
        rprint("[yellow] QC STATUS TABLE")
        self.logger_file.info("[yellow] QC STATUS TABLE")
        self.qcTable(self.sensorStatusList)

    def command_qv(self) -> tuple:
        """Query the Network Manager for GSM modem status.

        Issues a GET against :obj:`QC.server` + :obj:`QC.status_api`
        and surfaces the ``gsm`` key from the response.

        Returns:
            Tuple of ``(status_dict, http_status_code)``. On failure,
            returns ``({}, 404)``.

        Raises:
            requests.RequestException: Caught internally and logged.

        Example:
            Inspect GSM signal during diagnostics::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qv()  # doctest: +SKIP

        Note:
            Network Manager must be reachable and the modem powered.
        """
        try:
            r = requests.get(url=(self.server + self.status_api))
            status = json.loads(json.dumps(r.json()))
            rprint("GSM -> ", status["gsm"])
            self.logger_file.info(f"GSM -> {status['gsm']}")
            return (status, r.status_code)
        except Exception as e:
            rprint(e)
            self.logger_file.exception(e)
            return ({}, 404)

    def mapRange(
        self, value: float, inMin: float, inMax: float, outMin: float, outMax: float
    ) -> float:
        """Linearly map a value from one range to another.

        Args:
            value: Input value to map.
            inMin: Lower bound of the input range.
            inMax: Upper bound of the input range.
            outMin: Lower bound of the output range.
            outMax: Upper bound of the output range.

        Returns:
            Mapped value in the output range.

        Raises:
            ZeroDivisionError: If ``inMax == inMin``.

        Example:
            Convert a 0-100 percentage to an 8-bit PWM duty::

                >>> qc = QC()  # doctest: +SKIP
                >>> int(qc.mapRange(50, 0, 100, 0, 255))  # doctest: +SKIP
                127

        Note:
            Used by :meth:`.command_qe` to convert sensor counts to
            DAC counts for the 4-20 mA loop.
        """
        return outMin + (((value - inMin) / (inMax - inMin)) * (outMax - outMin))

    def command_qe(self) -> None:
        """Test the 4-20 mA current-loop DAC module (MCP4725) by sweeping output.

        Switches the I2C MUX to channel 0 via
        :meth:`drivers.gpio.gpio.select_I2C`, opens the MCP4725 at
        ``0x61``, then sweeps the DAC count from 700 to 3470 to drive
        the 4-20 mA loop across its full range, using
        :meth:`.mapRange` to translate the loop current back to a
        printable value.

        Returns:
            None. Output is logged for the operator's multimeter
            reference.

        Raises:
            OSError: Caught internally and logged.

        Example:
            Verify the 4-20 mA output module::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qe()  # doctest: +SKIP

        Note:
            Hardware-only; expects the MCP4725 DAC at ``0x61`` on bus 0.
        """
        try:
            self.logger_console.info("Checking 4-20 module")
            gpio.select_I2C(5)
            self.MCP4725 = Adafruit_MCP4725.MCP4725(address=0x61, busnum=0)
            self.min, self.max, self.dacmin, self.dacmax = 0, 80, 700, 3470
            for value in range(81):
                sendValue = self.mapRange(
                    value, self.min, self.max, self.dacmin, self.dacmax
                )
                currentValue = self.mapRange(
                    sendValue, self.dacmin, self.dacmax, 40, 200
                )
                self.logger_console.info(
                    f"[Current] sending value to 4-20mA loop {int(sendValue)}, {currentValue / 10}"
                )
                self.MCP4725.set_voltage(int(sendValue))
                time.sleep(0.2)
        except Exception as e:
            rprint(e)
            self.logger_file.exception(e)

    def command_qd(self) -> None:
        """Test the HMI (Nextion) display by sending page and icon updates.

        Opens a :class:`drivers.HMI.HMI.HMI` on ``/dev/ttyAMA3``,
        switches it to dev mode, sets a stub device ID, navigates to
        page 6, and exercises each status icon (battery, WiFi, GSM,
        location).

        Returns:
            None. Output appears on the Nextion HMI; operator must
            visually verify.

        Raises:
            serial.SerialException: Caught internally and logged.

        Example:
            Run the HMI smoke test::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qd()  # doctest: +SKIP

        Note:
            Hardware-only; expects a Nextion HMI wired to
            ``/dev/ttyAMA3`` at 115200 baud.
        """
        try:
            self.logger_console.info("HMI Test")
            hmi = HMI(hmi_port="/dev/ttyAMA3", baud=115200)
            hmi.dev = True
            hmi.heatingTime = 0
            hmi.updatedeviceId("PM01P0012")
            hmi.setup()
            hmi.showPage(6)
            self.logger_console.info("Listening [HMI] Waiting")
            time.sleep(3)
            self.logger_console.info(".")
            self.logger_console.info(".")
            self.logger_console.info(".")
            hmi.loop()
            hmi.updateBatteryData()
            hmi.updateBatteryIcon(1)
            hmi.updateWifiIcon(hmi.enable)
            hmi.updateBatteryIcon(hmi.enable)
            hmi.updateGSMIcon(hmi.enable)
            hmi.updateLocationIcon(hmi.enable)
        except Exception as e:
            rprint(e)
            self.logger_file.exception(e)

    def command_qt(self) -> bool:
        """Test an OGS sensor at a specific position with a user-selected type.

        Prompts for sensor type (``AS`` Alphasense, ``SM`` Semeatech,
        ``SM4`` Semeatech 4-byte, ``NN`` Nevada Nano) and slot index,
        builds the matching ``ogs`` config (part number plus baud
        rate), runs four read iterations, then prints the result.

        Args:
            None.

        Returns:
            ``True`` if the sensor produced valid data, ``False``
            otherwise.

        Raises:
            ValueError: Caught implicitly when an unknown sensor type
                is entered; the function still attempts to run.

        Example:
            Test an Alphasense OGS at slot 0::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_qt()  # enter 'AS' then '0' at prompts  # doctest: +SKIP

        Note:
            Hardware-only; expects an OGS rail on ``/dev/ttyAMA0``.
        """
        self.logger_console.info(" Sensor type is 'AS' for Alphasense sensor testing")
        self.logger_console.info(" Sensor type is 'SM' for Semeatech sensor testing")
        self.logger_console.info(" Sensor type is 'SM4' for Semeatech sensor testing")
        self.logger_console.info(" Sensor type is 'NN' for Nevada Nano sensor testing")
        sensorType = input("ENTER SENSOR TYPE: ")
        pos = input("ENTER SENSOR POSITION: ")
        if sensorType:
            sensorType = sensorType
        elif pos:
            pos = pos
        else:
            sensorType, partno, pos, baud = (
                "AS",
                101,
                0,
                115200,
            )  # Default sensor type - Alphasense, Position - 0
        sensor = OzOGS()
        if sensorType == "AS":
            partno, baud = 101, 115200
        elif sensorType == "SM":
            partno, baud = 104, 115200
        elif sensorType == "SM4":
            partno, baud = 105, 9600
        elif sensorType == "NN":
            partno, baud = 106, 38400
        else:
            partno, baud = 101, 115200  # default to Alphasense for unrecognized type
        config = {
            "ogs": [
                {
                    "en": 1,
                    "pn": partno,
                    "gpio": {
                        "pos": pos,
                        "baud": baud,
                        "port": "/dev/ttyAMA0",
                        "debug": 1,
                    },
                    "parameters": [
                        {"ch": 0, "cr": 0, "pm": 1, "sc": "g11", "se": 100},
                        {"ch": 1, "cr": 0, "pm": 2, "sc": "g12", "se": 100},
                    ],
                }
            ]
        }
        sensor.initialize(config["ogs"], self.init_value)
        for _ in track(range(4), description="[green]Sensor QC progress"):
            for _read in range(1):
                sensor.getSensorReading()
        data = {}
        sensorExists = False
        value = sensor.putsensorValue(data)
        sensorExists = False if value == {} else True
        if sensorExists == True:
            rprint("[green] Sensor data -> ", value)
            self.logger_file.info(f"[green] Sensor data -> {value}")
        elif sensorExists == False:
            rprint("[red]-----[ERR] Sensor not Found")
            self.logger_file.error("[red] Sensor not Found")
        return sensorExists

    def command_jj(self) -> None:
        """Display the hidden/diagnostic commands table.

        Renders :obj:`QC.rdCommands` via :meth:`.generate_table` so
        the operator can review every diagnostic command available
        beyond the standard QC workflow.

        Returns:
            None. Output is a Rich table printed to stdout.

        Raises:
            None.

        Example:
            Show all hidden commands::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.command_jj()  # doctest: +SKIP

        Note:
            For the regular command list see :meth:`.command_q`.
        """
        rprint("[yellow] HIDDEN COMMANDS TABLE")
        self.logger_file.info("[yellow] HIDDEN COMMANDS TABLE")
        self.generate_table(self.rdCommands)

    def sendDataToSocket(self, payload: dict) -> None:
        """POST sensor QC data to the Oizom Socket API.

        Builds the URL by concatenating :obj:`OIZOM_SOCKET` and
        :obj:`QC_API`, then sends ``payload`` as JSON with the bearer
        token in :obj:`QC.headers`. Response status, reason, and body
        are echoed to the console.

        Args:
            payload: Dictionary containing ``SensorId``, ``CommandId``,
                and ``SensorData`` fields.

        Returns:
            None. Side effects are limited to a single outbound HTTP
            POST.

        Raises:
            requests.RequestException: Propagated to the caller.

        Example:
            Send a captured SHT reading::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.sendDataToSocket({"SensorId": "ABC", "CommandId": "d", "SensorData": "..."})  # doctest: +SKIP

        Note:
            Bearer token in :obj:`QC.headers` is long-lived; rotate
            via deployment process.
        """
        r = requests.post(OIZOM_SOCKET + QC_API, json=payload, headers=self.headers)
        rprint(r.status_code, r.reason)
        rprint(r.text)

    def qcTable(self, dictt: dict) -> Table:
        """Render a Rich table showing sensor pass/fail status.

        Builds a :class:`rich.table.Table` with columns for serial
        number, command, and boolean status, then prints it to the
        Rich console and persists a copy via :meth:`.printTABLE_file`.

        Args:
            dictt: Dictionary mapping sensor short names to ``0``
                (fail) or ``1`` (pass).

        Returns:
            The rendered :class:`rich.table.Table` instance, returned
            for downstream callers that want to embed it in their own
            output.

        Raises:
            None.

        Example:
            Display the current sensor table::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.qcTable(qc.sensorStatusList)  # doctest: +SKIP

        Note:
            Side effect: writes the table to ``QC/table.txt`` via
            :meth:`.printTABLE_file`.
        """
        table = Table(style="cyan", highlight=True)
        table.add_column("Sr. No.", style="dim", width=7, justify="center")
        table.add_column("Command", width=15, justify="left")
        table.add_column("Status", width=10, justify="center")
        console = Console()
        i = 0
        for key, value in dictt.items():
            key = [key for key in dictt][i]
            value = [value for value in dictt.values()][i]
            key, value = str(key + " " + "test"), str(bool(value))
            table.add_row(str(i), key, value)
            i += 1
        console.print(table)
        self.printTABLE_file(table)
        return table

    def generate_table(self, dictt: dict) -> Table:
        """Render a Rich table from a command-to-description dictionary.

        Builds a :class:`rich.table.Table` with columns for serial
        number, command code, description, and a placeholder status,
        then prints it via :class:`rich.console.Console`.

        Args:
            dictt: Dictionary mapping command codes to description
                strings (e.g., :obj:`QC.qcCommands` or
                :obj:`QC.rdCommands`).

        Returns:
            The rendered :class:`rich.table.Table` instance.

        Raises:
            None.

        Example:
            Render the standard QC command list::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.generate_table(qc.qcCommands)  # doctest: +SKIP

        Note:
            Side effect: also writes the table to ``QC/table.txt``.
        """
        table = Table(style="cyan", highlight=True)
        table.add_column("Sr. No.", style="dim", width=7, justify="center")
        table.add_column("Command", justify="center")
        table.add_column("Description", justify="left")
        table.add_column("Status", justify="center")
        console = Console()
        i = 0
        for key, value in dictt.items():
            key = [key for key in dictt][i]
            value = [value for value in dictt.values()][i]
            key, value = str(key), str(value)
            table.add_row(str(i), key, value)
            i += 1
        console.print(table)
        self.printTABLE_file(table)
        return table

    def comm_a(self) -> bool:
        """Run the full motherboard (MB) test suite.

        Walks :obj:`QC.mbCommands` and dispatches each entry through
        :meth:`.indirect`. Includes LED, RTC, SHT, BME, battery,
        light-UV, and fan tests. Aggregates pass/fail into
        :obj:`QC.sensorStatusList` and prints the final table.

        Returns:
            ``True`` when the motherboard test suite completes. The
            value is also stored on :obj:`QC.mbFlag`.

        Raises:
            None. Per-test exceptions are caught and logged.

        Example:
            Kick off the MB suite::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.comm_a()  # doctest: +SKIP

        Note:
            Hardware-only; usually invoked from :meth:`.get_input`
            when the operator enters ``a``.
        """
        rprint("[green] MB testing started")
        self.logger_file.info("[green] MB testing started")
        rprint(
            "[yellow] ++ Includes LED test, SHT, BME, AHT20, SEN66, Battery/MPPT, Light-UV, Fan test"
        )
        self.logger_file.info(
            "[yellow] ++ Includes LED test, SHT, BME, AHT20, SEN66, Battery/MPPT, Light-UV, Fan test"
        )
        for key, value in self.mbCommands.items():
            rprint(str(key))
            self.logger_file.info(str(key))
            try:
                self.indirect(value)
            except Exception as e:
                rprint(e)
                self.logger_file.exception(e)
        rprint("[yellow] QC STATUS TABLE")
        self.logger_file.info("[yellow] QC STATUS TABLE")
        self.qcTable(self.sensorStatusList)
        self.mbFlag = True
        rprint("QC MB Flag - ", self.mbFlag)
        self.logger_file.info(f"QC MB Flag - {self.mbFlag}")
        return self.mbFlag

    def comm_ha(self) -> bool:
        """Run the Helium motherboard test suite (LED, RTC, SHT, light-UV, fan).

        Iterates :obj:`QC.helium_mbCommands` through :meth:`.indirect`
        and prunes :obj:`QC.sensorStatusList` to remove sensors that
        are not present on the Helium variant (BME, battery, noise,
        wind, rain).

        Returns:
            ``True`` when the Helium MB test suite completes. The
            value is also stored on :obj:`QC.mbFlag`.

        Raises:
            None. Per-test exceptions are caught and logged.

        Example:
            Run the Helium MB suite::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.comm_ha()  # doctest: +SKIP

        Note:
            Hardware-only; populates :obj:`QC.helium_sensorList` as a
            pruned copy of :obj:`QC.sensorStatusList`.
        """
        rprint("[green] Helium MB testing started")
        self.logger_file.info("[green] Helium MB testing started")
        rprint("[yellow] ++ Includes LED test, RTC, SHT, Light-UV, Fan test")
        self.logger_file.info(
            "[yellow] ++ Includes LED test, RTC, SHT, Light-UV, Fan test"
        )
        for key, value in self.helium_mbCommands.items():
            rprint(str(key))
            self.logger_file.info(str(key))
            try:
                self.indirect(value)
            except Exception as e:
                rprint(e)
                self.logger_file.exception(e)
        rprint("[yellow] QC STATUS TABLE")
        self.logger_file.info("[yellow] QC STATUS TABLE")
        self.helium_sensorList = self.sensorStatusList.copy()
        # Removing unrequired sensors from the list for Helium MB QC
        self.helium_sensorList.pop("bme", None)
        self.helium_sensorList.pop("aht20", None)
        self.helium_sensorList.pop("batt", None)
        self.helium_sensorList.pop("noise", None)
        self.helium_sensorList.pop("wind", None)
        self.helium_sensorList.pop("rain", None)
        self.qcTable(self.helium_sensorList)
        self.mbFlag = True
        rprint("QC MB Flag - ", self.mbFlag)
        self.logger_file.info(f"QC MB Flag - {self.mbFlag}")
        return self.mbFlag

    def comm_b(self) -> bool:
        """Run the DNS (Dust-Noise-Sensors) test suite.

        Walks :obj:`QC.dnsCommands` through :meth:`.indirect`,
        running the MB sensor set plus dust, noise, and wind.

        Returns:
            ``True`` when the DNS test suite completes. The value is
            also stored on :obj:`QC.dnsFlag`.

        Raises:
            None. Per-test exceptions are caught and logged.

        Example:
            Kick off the DNS suite::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.comm_b()  # doctest: +SKIP

        Note:
            Hardware-only; must be run after :meth:`.comm_a` to
            unlock HOLO allocation via :meth:`.comm_c`.
        """
        rprint("[green] DNS testing started")
        self.logger_file.info("[green] DNS testing started")
        rprint(
            "[yellow] ++ Includes LED test, SHT, BME, AHT20, SEN66, Battery/MPPT, Light-UV, Fan test"
        )
        self.logger_file.info(
            "[yellow] ++ Includes LED test, SHT, BME, AHT20, SEN66, Battery/MPPT, Light-UV, Fan test"
        )
        rprint("[yellow] ++ Cubic Dust, Noise sensor, Wind Rain test")
        self.logger_file.info("[yellow] ++ Cubic Dust, Noise sensor, Wind Rain test")
        for key, value in self.dnsCommands.items():
            rprint(str(key))
            self.logger_file.info(str(key))
            try:
                self.indirect(value)
            except Exception as e:
                rprint(e)
                self.logger_file.exception(e)
        rprint("[yellow] QC STATUS TABLE")
        self.logger_file.info("[yellow] QC STATUS TABLE")
        self.qcTable(self.sensorStatusList)
        self.dnsFlag = True
        rprint("QC DNS Flag - ", self.dnsFlag)
        self.logger_file.info(f"QC DNS Flag - {self.dnsFlag}")
        return self.dnsFlag

    def comm_hb(self) -> bool:
        """Run the Helium DNS test suite (SHT, light-UV, fan, dust).

        Walks :obj:`QC.helium_dnsCommands` through :meth:`.indirect`
        and renders the pruned :obj:`QC.helium_sensorList` table.

        Returns:
            ``True`` when the Helium DNS test suite completes. The
            value is also stored on :obj:`QC.dnsFlag`.

        Raises:
            None. Per-test exceptions are caught and logged.

        Example:
            Kick off the Helium DNS suite::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.comm_hb()  # doctest: +SKIP

        Note:
            Hardware-only; must run after :meth:`.comm_ha` so that
            :obj:`QC.helium_sensorList` exists.
        """
        rprint("[green]Helium DNS testing started")
        self.logger_file.info("[green]Helium DNS testing started")
        rprint("[yellow] ++ Includes LED test, SHT, Light-UV, Fan test")
        self.logger_file.info("[yellow] ++ Includes LED test, SHT, Light-UV, Fan test")
        rprint("[yellow] ++ Cubic Dust")
        self.logger_file.info("[yellow] ++ Cubic Dust")
        for key, value in self.helium_dnsCommands.items():
            rprint(str(key))
            self.logger_file.info(str(key))
            try:
                self.indirect(value)
            except Exception as e:
                rprint(e)
                self.logger_file.exception(e)
        rprint("[yellow] QC STATUS TABLE")
        self.logger_file.info("[yellow] QC STATUS TABLE")
        self.qcTable(self.helium_sensorList)
        self.dnsFlag = True
        rprint("QC DNS Flag - ", self.dnsFlag)
        self.logger_file.info(f"QC DNS Flag - {self.dnsFlag}")
        return self.dnsFlag

    def comm_c(self) -> bool | None:
        """Run the full HOLO allocation test suite and register the device.

        Walks :obj:`QC.holoCommands` through :meth:`.indirect`,
        renders the status table, then if at least one sensor passed
        (or :obj:`QC.qcBypass` is set) prompts the operator for the
        Particle ID and Aikaan DM ID and POSTs them to
        :obj:`OIZOM_MANAGER` + :obj:`HOLO_API`.

        Returns:
            ``True`` if QC passed and the device was registered.
            ``None`` if a sensor failure prevented registration.

        Raises:
            requests.RequestException: Propagated from the registration
                POST when network errors occur.

        Example:
            Allocate a freshly built HOLO board::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.comm_c()  # doctest: +SKIP

        Note:
            Hardware-only; expects :meth:`.comm_a` and :meth:`.comm_b`
            to have already run successfully.
        """
        rprint("[green] HOLO testing started")
        self.logger_file.info("[green] HOLO testing started")
        rprint(
            "[yellow] ++ Includes LED test, SHT, BME, AHT20, SEN66, Battery/MPPT, Light-UV, Fan test"
        )
        self.logger_file.info(
            "[yellow] ++ Includes LED test, SHT, BME, AHT20, SEN66, Battery/MPPT, Light-UV, Fan test"
        )
        rprint("[yellow] ++ Cubic Dust, Noise sensor, Wind - Rain test")
        self.logger_file.info("[yellow] ++ Cubic Dust, Noise sensor, Wind - Rain test")
        rprint("[yellow] ++ Display, Siren, OGS, & CO2 test ")
        self.logger_file.info("[yellow] ++ Display, Siren, OGS, & CO2 test ")
        for key, value in self.holoCommands.items():
            rprint(str(key))
            self.logger_file.info(str(key))
            try:
                self.indirect(value)
            except Exception as e:
                rprint(e)
                self.logger_file.exception(e)
        rprint("[yellow] QC STATUS TABLE")
        self.logger_file.info("[yellow] QC STATUS TABLE")
        self.qcTable(self.sensorStatusList)
        self.logger_console.info(
            f"[yellow] Sum Sensor Status values: {sum(self.sensorStatusList.values())}"
        )
        if sum(self.sensorStatusList.values()) or self.qcBypass:
            particleId = input("Please Enter Particle Id:> ")
            rprint("Particle-id: ", particleId)
            self.logger_file.info(f"Particle-id: {particleId}")
            dmid = input("Enter DM id from Aikaan platform:> ")
            rprint("DM-id: ", dmid)
            self.logger_file.info(f"DM-id: {dmid}")
            rprint("Sensor List: ", self.sensorStatusList)
            self.logger_file.info(f"Sensor List: {self.sensorStatusList}")
            payload = {"dt": 2, "dmid": dmid}
            r = requests.post(
                OIZOM_MANAGER + HOLO_API + particleId,
                json=payload,
                headers=self.headers,
            )
            rprint(r.status_code, r.reason)
            self.logger_file.info(f"{r.status_code} {r.reason}")
            rprint(r.text)
            self.logger_file.info(r.text)
            rprint(f"Well done. You have completed the QC of {particleId} ZULU Board.")
            self.logger_file.info(
                f"Well done. You have completed the QC of {particleId} ZULU Board."
            )
            rprint("[yellow] QC STATUS TABLE")
            self.logger_file.info("[yellow] QC STATUS TABLE")
            self.qcTable(self.sensorStatusList)
            self.qcFlag = True
            rprint("QC Flag - ", self.qcFlag)
            self.logger_file.info(f"QC Flag - {self.qcFlag}")
            return self.qcFlag
        rprint(
            "[red] FAULTY SENSOR - system will not go ahead if any one of the sensors is faulty, please use working sensor during QC.!"
        )
        self.logger_file.info(
            "[red] FAULTY SENSOR - system will not go ahead if any one of the sensors is faulty, please use working sensor during QC.!"
        )

    def comm_hc(self) -> bool | None:
        """Run the Helium HOLO allocation test suite and register the device.

        Walks :obj:`QC.helium_holoCommands` through :meth:`.indirect`,
        renders :obj:`QC.helium_sensorList`, and if at least one
        sensor passed (or :obj:`QC.qcBypass` is set), prompts for the
        Particle ID and Aikaan DM ID, then registers the device via
        :obj:`OIZOM_MANAGER` + :obj:`HOLO_API`.

        Returns:
            ``True`` if QC passed and the device was registered.
            ``None`` if a sensor failure prevented registration.

        Raises:
            requests.RequestException: Propagated from the registration
                POST when network errors occur.

        Example:
            Allocate a freshly built Helium HOLO board::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.comm_hc()  # doctest: +SKIP

        Note:
            Hardware-only; depends on the upstream Oizom Manager being
            reachable from the bridge gateway.
        """
        rprint("[green]Helium HOLO testing started")
        self.logger_file.info("[green]Helium HOLO testing started")
        rprint("[yellow] ++ Includes LED test, SHT, Light-UV, Fan test")
        self.logger_file.info("[yellow] ++ Includes LED test, SHT, Light-UV, Fan test")
        rprint("[yellow] ++ Cubic Dust")
        self.logger_file.info("[yellow] ++ Cubic Dust")
        rprint("[yellow] ++ Display, Siren, OGS, & CO2 test ")
        self.logger_file.info("[yellow] ++ Display, Siren, OGS, & CO2 test ")
        for key, value in self.helium_holoCommands.items():
            rprint(str(key))
            self.logger_file.info(str(key))
            try:
                self.indirect(value)
            except Exception as e:
                rprint(e)
                self.logger_file.exception(e)
        rprint("[yellow] QC STATUS TABLE")
        self.logger_file.info("[yellow] QC STATUS TABLE")
        self.qcTable(self.helium_sensorList)
        self.logger_console.info(
            f"[yellow] Sum Sensor Status values: {sum(self.helium_sensorList.values())}"
        )
        if sum(self.helium_sensorList.values()) or self.qcBypass:
            particleId = input("Please Enter Particle Id:> ")
            rprint("Particle-id: ", particleId)
            self.logger_file.info(f"Particle-id: {particleId}")
            dmid = input("Enter DM id from Aikaan platform:> ")
            rprint("DM-id: ", dmid)
            self.logger_file.info(f"DM-id: {dmid}")
            rprint("Sensor List: ", self.helium_sensorList)
            self.logger_file.info(f"Sensor List: {self.helium_sensorList}")
            payload = {"dt": 2, "dmid": dmid}
            r = requests.post(
                OIZOM_MANAGER + HOLO_API + particleId,
                json=payload,
                headers=self.headers,
            )
            rprint(r.status_code, r.reason)
            self.logger_file.info(f"{r.status_code} {r.reason}")
            rprint(r.text)
            self.logger_file.info(r.text)
            rprint(
                f"Well done. You have completed the QC of {particleId} Helium Board."
            )
            self.logger_file.info(
                f"Well done. You have completed the QC of {particleId} Helium Board."
            )
            rprint("[yellow] QC STATUS TABLE")
            self.logger_file.info("[yellow] QC STATUS TABLE")
            self.qcTable(self.helium_sensorList)
            self.qcFlag = True
            rprint("QC Flag - ", self.qcFlag)
            self.logger_file.info(f"QC Flag - {self.qcFlag}")
            return self.qcFlag
        rprint(
            "[red] FAULTY SENSOR - system will not go ahead if any one of the sensors is faulty, please use working sensor during QC.!"
        )
        self.logger_file.info(
            "[red] FAULTY SENSOR - system will not go ahead if any one of the sensors is faulty, please use working sensor during QC.!"
        )

    def comm_e5(self) -> None:
        """Read and display the Grove LoRa-E5 DevEUI and AppEUI in hex and decimal.

        Opens a :class:`drivers.LORAE5.LORAE5.LORAE5` instance on
        ``/dev/ttyAMA2`` at 9600 baud, issues a probe ``AT``, then
        reads the device EUI and application EUI, prints both as
        colon-separated hex and as decimal sequences.

        Returns:
            None. Output is logged to the console for the operator.

        Raises:
            serial.SerialException: Propagated when the LoRa module is
                unreachable.

        Example:
            Read the EUIs from a Grove LoRa-E5::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.comm_e5()  # doctest: +SKIP

        Note:
            Hardware-only; assumes the LoRa-E5 module is wired to UART
            ``/dev/ttyAMA2``.
        """
        self.logger_console.info("Grove LoRa E5 DevEUI and AppEUI Check...")
        lora = LORAE5(lora_port="/dev/ttyAMA2", lora_baud=9600)
        lora.lora.send_command("AT")
        lora_status = lora.lora.readResponse(400)
        if lora_status and ":" in lora_status:
            lora_status = lora_status.split(":")[1].strip()
            if lora_status == "OK":
                self.logger_file.info(
                    "LORA Module is Sending and receiving AT Commands"
                )
                rprint("[green] LORA Module is Sending and receiving AT Commands")
            else:
                self.logger_file.info(
                    f"LORA Module is not Sending correct response. RESPONSE: {lora_status}"
                )
                rprint(
                    f"LORA Module is not Sending correct response. RESPONSE: {lora_status}"
                )
                return
        else:
            self.logger_file.info("LORA Module is not Working. No Response Received")
            rprint("[red] LORA Module is not Working. No Response Received")
            return
        deveui = lora.lora.getDevEUI()
        deveui = deveui.split(",")[1].strip()
        deveui = deveui.replace(":", ",")
        self.logger_file.info(f"DEVEUI HEX: {deveui}")
        rprint(f"[green] DEVEUI HEX: {deveui}")
        deveui_hex = deveui.split(",")
        deveui_decimal = [int(val, 16) for val in deveui_hex]
        decimal_string = ",".join(map(str, deveui_decimal))
        self.logger_file.info(f"DEVEUI DEC: {decimal_string}")
        rprint(f"[green] DEVEUI DEC: {decimal_string}")

        appeui = lora.lora.getAppEUI()
        appeui = appeui.split(",")[1].strip()
        appeui = appeui.replace(":", ",")
        self.logger_file.info(f"APPEUI HEX: {appeui}")
        rprint(f"[green] APPEUI HEX: {appeui}")
        appeui_hex = appeui.split(",")
        appeui_decimal = [int(val, 16) for val in appeui_hex]
        decimal_string = ",".join(map(str, appeui_decimal))
        self.logger_file.info(f"APPEUI DEC: {decimal_string}")
        rprint(f"[green] APPEUI DEC: {decimal_string}")

    # def printLOGO(self) -> None:
    #     """Print the Hydroxy ASCII art logo from ``QC/logo.txt``.

    #     Opens ``QC/logo.txt`` relative to the working directory,
    #     reads the contents, and renders them in white via
    #     :func:`rich.print`.

    #     Returns:
    #         None. Output is purely cosmetic.

    #     Raises:
    #         FileNotFoundError: If ``QC/logo.txt`` is missing.

    #     Example:
    #         Print the logo at startup::

    #             >>> qc = QC()  # doctest: +SKIP
    #             >>> qc.printLOGO()  # doctest: +SKIP

    #     Note:
    #         Path is relative; run from the project root for the file
    #         lookup to resolve correctly.
    #     """
    #     f = open("QC/logo.txt")
    #     content = f.read()
    #     rprint(f"[white] {content}")  # can change the logo color from here
    #     # self.logger_file.info(content)                    # To print logo in app.log file

    def printTABLE_file(self, table: Table) -> None:
        """Write a Rich table to ``QC/table.txt`` and append it to the log file.

        Re-renders the table via a file-backed
        :class:`rich.console.Console`, reads the result back, and
        forwards it to :obj:`QC.logger_file`. Useful for archiving
        the final status table next to the running log.

        Args:
            table: The :class:`rich.table.Table` to persist.

        Returns:
            None. Side effect: ``QC/table.txt`` is overwritten and the
            file logger receives the rendered string.

        Raises:
            OSError: If ``QC/table.txt`` is not writable.

        Example:
            Persist the final status table::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.printTABLE_file(qc.qcTable(qc.sensorStatusList))  # doctest: +SKIP

        Note:
            Called automatically by :meth:`.qcTable` and
            :meth:`.generate_table`; not usually invoked directly.
        """
        # Define the log file name
        table_path = "QC/table.txt"

        # Create a console object
        f = open(table_path, "w")
        console = Console(file=f)
        console.print(table)
        f.close()

        f = open(table_path)
        content = f.read()
        content = "\n" + content
        self.logger_file.info(content)
        f.close()

    def millis(self) -> int:
        """Return the current time in milliseconds since the Unix epoch.

        Returns:
            Current wall-clock time, in milliseconds, as an
            :class:`int`. Useful for low-resolution timing inside
            sensor loops.

        Raises:
            None.

        Example:
            Measure a short delay::

                >>> qc = QC()  # doctest: +SKIP
                >>> start = qc.millis()  # doctest: +SKIP

        Note:
            Wraps :func:`time.time`; precision is limited by the
            underlying OS clock.
        """
        return int(time.time() * 1000)

    def get_input(self, prompt: str) -> str:
        """Prompt the operator for a QC command and dispatch it.

        Reads a line from ``stdin``, looks it up in
        :obj:`QC.qcCommands` or :obj:`QC.rdCommands` for logging,
        delegates the actual work to :meth:`.indirect`, and finally
        runs any aggregate suites (``a``, ``ha``, ``b``, ``hb``,
        ``e5``, ``c``, ``hc``) directly. ``c`` and ``hc`` are gated
        on :obj:`QC.mbFlag` and :obj:`QC.dnsFlag`.

        Args:
            prompt: Input prompt string shown to the operator.

        Returns:
            The command string entered by the operator. Returns the
            previously bound ``command`` even when ``input()`` raises
            EOF, since exceptions are caught and logged.

        Raises:
            None. Exceptions are caught internally and logged.

        Example:
            Drive a single QC turn::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.get_input("Enter Command:> ")  # doctest: +SKIP

        Note:
            Blocks on ``input()`` so this method is only suitable for
            interactive use, not automated scripts.
        """
        try:
            command = input(prompt)
        except EOFError as e:
            rprint(e)
            self.logger_file.exception(e)
            return None
        try:
            if command in self.qcCommands:
                rprint(
                    f"[green]Entered command: {command}, Sensor: {self.qcCommands[command]}"
                )
                self.logger_file.info(
                    f"[green]Entered command: {command}, Sensor: {self.qcCommands[command]}"
                )
            elif command in self.rdCommands:
                rprint(
                    f"[green]Entered command: {command}, Sensor: {self.rdCommands[command]}"
                )
                self.logger_file.info(
                    f"[green]Entered command: {command}, Sensor: {self.rdCommands[command]}"
                )
            # elif command in self.extraCommands:
            #     rprint(f"[green]Entered command: {command}, Sensor: {self.extraCommands[command]}")
            #     self.logger_file.info(f"[green]Entered command: {command}, Sensor: {self.extraCommands[command]}")
            self.indirect(command)
        except Exception as e:
            rprint(f"[red] {e}")
            self.logger_file.exception(e)
        if command == "a":
            self.comm_a()
        elif command == "ha":
            self.comm_ha()
        elif command == "b":
            self.comm_b()
        elif command == "hb":
            self.comm_hb()
        elif command == "e5":
            self.comm_e5()
        elif command == "c" and self.mbFlag == True and self.dnsFlag == True:
            self.qcStatus = self.comm_c()
            if self.qcStatus:
                rprint("[green] QC Completed. Please reboot the device")
                self.logger_file.info("[green] QC Completed. Please reboot the device")
        elif command == "hc" and self.mbFlag == True and self.dnsFlag == True:
            self.qcStatus = self.comm_hc()
            if self.qcStatus:
                rprint("[green]Helium QC Completed. Please reboot the device")
                self.logger_file.info(
                    "[green]Helium QC Completed. Please reboot the device"
                )
        elif command == "c" or command == "hc":
            rprint("[red] Complete the QC first")
            self.logger_file.info("[red] Complete the QC first")
        return command

    def parse_arguments(self, args: list[str]) -> None:
        """Parse CLI arguments and run the corresponding QC tests.

        Iterates over ``args`` and, for each token that matches a key
        in :obj:`QC.argsParse`, dispatches the mapped command code via
        :meth:`.indirect`. Unrecognised tokens are silently ignored.

        Args:
            args: List of command-line argument strings (e.g.,
                ``["--sht", "--bme"]``).

        Returns:
            None. Each matched test runs in turn and prints its own
            status.

        Raises:
            None. Per-test exceptions are caught and logged.

        Example:
            Run the harness from the script entry point::

                >>> qc = QC()  # doctest: +SKIP
                >>> qc.parse_arguments(["--sht", "--bme"])  # doctest: +SKIP

        Note:
            Typically called from ``__main__`` with :obj:`sys.argv`.
        """
        try:
            for i in range(len(args)):
                search_param = args[i]
                # if len(args) == 1:                                    # if only one arguement is parsed, it shall follow automatically
                for key, value in self.argsParse.items():
                    if key == search_param:
                        self.indirect(
                            value
                        )  # call the value by comparing key with args
        except Exception as e:
            rprint(e)
            self.logger_file.exception(e)


if __name__ == "__main__":
    from QC.QC import QC

    qc = QC()
    import sys

    args = sys.argv
    qc.parse_arguments(args)
    # qc.printLOGO()
    qc.command_q()

    while True:
        try:
            answer = qc.get_input("Enter Command:> ")
            if answer == "x":
                break
        except Exception as e:
            qc.logger_console.exception(e)
    qc.logger_console.info("---------- QC file ends ----------")
