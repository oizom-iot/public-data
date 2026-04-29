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
from rich import print as rprint
from rich.console import Console
from rich.progress import track
from rich.table import Table

from drivers.gpio import gpio
from drivers.HMI.HMI import HMI
from drivers.LORAE5.LORAE5 import LORAE5
from drivers.MCP230XX import MCP230XX
from drivers.Noise.Noise import Noise
from drivers.Rain.Rain import Rain
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
from OzWrapper.OzRain import OzRain
from OzWrapper.OzRGB import OzRGB
from OzWrapper.OzSamd import OzSamd
from OzWrapper.OzSystem import OzSystem
from OzWrapper.OzTemp import OzTemp
from OzWrapper.OzUVLight import OzUVLight
from OzWrapper.OzWind import OzWind

OIZOM_MANAGER = "http://manager.oizom.com"
OIZOM_SOCKET = "http://socket.oizom.com"
HOLO_API = "/v2/qc/init/"
QC_API = "/qc/sensor/data"

serial_port = "/dev/ttyACM0"
samd_serial = serial.Serial(
    port=serial_port,
    baudrate=115200,
    timeout=5,
    write_timeout=3,
)


class QC:
    # Class #QC variables
    config = {}

    sensor = None
    MCP = None
    network = Network(timeout=6)
    network_status = Queue(2)
    Led_rgb = OzRGB
    samd = None
    oztemp = None
    ozbatt = None
    ozgps = None
    ozdust = None
    ozogs = None
    ozuvlight = None
    ozflood = None
    ozsystem = None
    tm1637 = None
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

    samdv2 = ["noise2", "wind2", "rain2"]

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
        "Battery test": "f",
        "Light-UV test": "k",
        "Fan test": "l",
    }
    helium_mbCommands = {
        "LED test": "i",
        "RTC Test": "qg",
        "SHT test": "d",
        "Light-UV test": "k",
        "Fan test": "l",
    }
    dnsCommands = {
        "LED test": "i",
        "SHT test": "d",
        "BME test": "e",
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
        "Battery test": "f",
        "Light-UV test": "k",
    }
    uartComands = {"Cubic Test": "h", "OGS Test": "j"}
    samdCommands = {"Noise Test": "n", "Wind Test": "o", "Rain Test": "p"}

    argsParse = {
        "--sht": "d",
        "--bme": "e",
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
        "--allOGS": "af",
        "--allOGS": "sb",
        "--checkrtc": "qg",
        "--heliumsiren": "hz",
    }
    qcCommands = {
        "a": "MotherBoard Test (Zulu-Board)",
        "ha": "Helium Board Test",
        "b": "DNS Test (Dust-Noise-&Sensors)",
        "hb": "Helium DNS Test (Dust-Noise-&Sensors)",
        "c": "HOLO Test (with Ethernet)",
        "hc": "Helium HOLO Test (with Ethernet)",
        "d": "SHT31 Test",
        "e": "BME280 Test",
        "f": "Battery Test",
        "g": "GPS Test",
        "h": "Dust Test",
        "i": "LED Test",
        "j": "OGS Test",
        "k": "Light-UV35 Test",
        "cd": "Dust Calibration",
        "l": "Fan Test",
        "m": "Network Test",
        "n": "Noise Test",
        "n2": "Noise2 Test",
        "o": "Wind Test",
        "o2": "Wind2 Test",
        "p": "Rain Test",
        "p2": "Rain2 Test",
        "q": "QC checklist",
        "r": "Flood Test",
        "s": "Relay Test",
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
        "ae": "ATHP test",
        "af": "All positions OGS test",
        "sb": "All positions OGS test",
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
        "--allOGS": "All positions OGS test for sensorboard",
        "--checkrtc": "Check RTC",
    }
    qcSensorList = {
        "d": "sht31",
        "e": "bme280",
        "f": "batt",
        "g": "gps",
        "h": "dust",
        "j": "ogs",
        "k": "lightuv",
        "n": "noise",
        "n2": "noise2",
        "o": "wind",
        "o2": "wind2",
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
        "ae": "athp",
        "af": "allOGS",
        "sb": "allOGS",
        "ag": "uv36",
        "ah": "sht31",
        "as": "AS3935",
    }

    """
    sht31   -> OzTemp()
            -> temp config
            -> initialize ['temp']
            -> update sensor list ['sht'] 
    """

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
    athp = (
        {
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
        },
    )
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

    def __init__(self):
        port = "8084"
        colon = ":"
        ip = subprocess.check_output(["hostname", "-i"])
        # print("The Hardware IP is: {}".format(out))
        ip = ip.decode("utf-8").strip(
            "\n"
        )  # convert response from bytes to string and drop \n
        gatewayIP = ip.replace(ip[len(ip) - 1 :], "1")  # update the last IP digit to 1
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

    def setup_logger_console(self):
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

    def setup_logger_file(self):
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

    def indirect(self, i):
        i = i.split(" ")
        self.i = i
        if i[0] not in self.qcSensorList.keys():
            method_name: str = "command_" + str(i[0])
            method = getattr(self, method_name, lambda: "Invalid")
            return method()
        sensor_name = self.qcSensorList[i[0]]
        method = getattr(self, sensor_name, lambda: "Invalid")
        self.sensor = self.sensorClass(method["class"])
        init = method["init"]
        sensor = method["sensor"]
        rprint(f"[HydroQC] Config: {init}")
        self.logger_file.info(f"[HydroQC] Config: {init}")
        if sensor == "gps" or sensor == "system":
            self.sensor.initialize(init)

        elif sensor == "wind2":
            self.sensor.initialize(samd_serial, init[0])

        elif sensor == "rain2":
            self.sensor.initialize(samd_serial, init[0])

        elif sensor == "noise2":
            self.sensor.initialize(samd_serial, init[0])

        else:
            self.sensor.initialize(init, self.init_value)
        for _ in track(range(4), description="[green]Sensor QC progress"):
            for i in range(0, 1):
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
        if sensorExists == True:
            rprint("[green] Sensor data: ", value)
            self.logger_file.info(f"[green] Sensor data: {value}")
        elif sensorExists == False:
            rprint(f"[red]   [ERR] {sensor} Sensor not Found/not Working")
            self.logger_file.error(f"[red] {sensor} Sensor not Found/not Working")
        self.logger_console.info(f"Sensor Exist: {sensorExists}")
        return sensorExists

    def sensorClass(self, sensor):
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
        if sensor == "wind2":
            return Wind()
        if sensor == "rain2":
            return Rain()
        if sensor == "noise2":
            return Noise()
        if sensor == "ozlightning":
            return OzLightning()

    # Sensor data validation to perform the true false method
    def sensorDataValidation(self, sensorType):
        data = {}
        sensorExists = False
        sensor = sensorType
        value = self.sensor.putsensorValue(data)
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

    # #LED testing
    def command_i(self):
        for i in range(len(self.ledStatus)):
            self.network_status.queue[0] = i
            rprint("LED Status -> ", self.ledStatus[i])
            self.logger_file.info(f"LED Status -> {self.ledStatus[i]}")
            time.sleep(1)

    def readFAN(self, fan_delay):
        status1, status2 = [], []
        time_prev = int(time.time())
        while fan_delay > (int(time.time()) - time_prev):
            status1.append(self.MCP.input(self.MCP.PIN_A))
            status2.append(self.MCP.input(self.MCP.PIN_B))
        err1 = sum(status1) / len(status1)
        err2 = sum(status2) / len(status2)
        self.logger_console.info(f"[FAN] {err1}, {err2}")
        if err1 == 0 or err1 == 1 or err2 == 0 or err2 == 1:
            self.MCP.digitalWrite(self.MCP.FAN_STATUS, 0)
            return False
        self.MCP.digitalWrite(self.MCP.FAN_STATUS, 1)
        return True

    # #Fan testing
    def command_l(self):
        self.MCP = MCP230XX(devicenumber=os.getenv("MCP_ID", 6))
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

    # Network testing
    def command_m(self) -> json:
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
        except Exception as e:
            rprint(e)
            self.logger_file.exception(e)
            return ({}, 404)

    # SAMD Upload firmware
    # to download Lasan github firmware - will have to login to github to download
    def command_qo(self):
        # print("[QC] pass - no code")
        try:
            ip_address = self.findBridgeIP()
            url = ip_address + self.samd_api
            self.logger_console.info(url)
            binary_file_path = "/usr/src/app/QC/Samdfirmware_v3.bin"
            firmware_bin = "/usr/src/app/QC/Samdfirmware_v3.bin"
            files = {"firmware_bin": open(binary_file_path, "rb")}
            self.logger_console.info(
                f"QC file size: {os.path.getsize(binary_file_path)}"
            )
            try:
                self.response = requests.post(
                    url, files=firmware_bin, headers=self.headers
                )
            except Exception as e:
                rprint(e)
                self.logger_file.exception(e)
            status = json.loads(json.dumps(self.response.json()))
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

    def command_qj(self):
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

    def command_qh(self):
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

    def command_qi(self):
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

    def findBridgeIP(self):
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

    def command_qg(self):
        try:
            flag = 0
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

    def command_qf(self):
        self.qcBypass = True
        return self.qcBypass

    def command_exe(self):
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

    # Print args parse
    def command_help(self):
        self.logger_console.info(self.argsParse)
        # print(self.argsParse)

    # Reboot System
    def command_ql(self):
        self.samd.initialize(self.samdWatchdog["samd"], self.init_value)
        self.samd.rebootSystem()
        rprint("Rebooting System.. Signing Off.. Bye Bye!!!")
        self.logger_file.info("Rebooting System.. Signing Off.. Bye Bye!!!")
        return True

    # Reboot 3v3
    def command_qn(self):
        self.MCP = MCP230XX(devicenumber=os.getenv("MCP_ID", 6))
        self.MCP.resetDefault()
        rprint("Rebooting 3.3V")
        self.logger_file.info("Rebooting 3.3V")
        self.MCP.power_3v3_rst()
        rprint("Reboot completed")
        self.logger_file.info("Reboot completed")
        return True

    # Reboot 5v
    def command_qm(self):
        self.MCP = MCP230XX(devicenumber=os.getenv("MCP_ID", 6))
        self.MCP.resetDefault()
        rprint("Rebooting 5V")
        self.logger_file.info("Rebooting 5V")
        self.MCP.power_5v_rst()
        rprint("Reboot completed")
        self.logger_file.info("Reboot completed")
        return True

    # QC Checklist
    def command_q(self):
        rprint("[yellow] QC COMMAND-LIST TABLE")
        self.logger_file.info("[yellow] QC COMMAND-LIST TABLE")
        self.generate_table(self.qcCommands)
        return

    # Relay test
    def command_s(self):
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

    # Display testing
    def command_y(self):
        CLK, DIO = 21, 20
        self.MCP = MCP230XX(devicenumber=os.getenv("MCP_ID", 6))
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

    # Display testing
    def command_hy(self):
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

    # Display Message printing
    def command_qk(self):
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

        # Dust Calibration Process

    def command_cd(self):
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
        for i in range(0, 2):
            dustCal.getSensorReading()
        data = {}
        print("[dustCal] PutSensor value start \n\n")
        print(dustCal.putsensorValue(data))
        return True

    # Buzzer / Siren testing
    def command_z(self):
        self.MCP = MCP230XX(devicenumber=os.getenv("MCP_ID", 6))
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
        try:
            from OzWrapper.OzRGB.OzRGB import get_color_from_value, hex_to_rgb, pixels
        except ImportError as e:
            rprint("[red] Beacon test dependencies missing or import failed.")
            self.logger_file.error(f"Beacon test import error: {e}")

        test_values = [10, 50, 100, 200, 300, 400]

        for value in test_values:
            color = get_color_from_value(value)
            rgb = hex_to_rgb(color)

            pixels[:] = [rgb] * len(pixels)
            pixels.show()

            print(f"Test value {value} -> {rgb}")
            time.sleep(2)

        return True

    # Helium Buzzer / Siren testing
    def command_hz(self):
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

    # Command QZ - checking the sensor List
    def command_qz(self):
        rprint("[yellow] QC STATUS TABLE")
        self.logger_file.info("[yellow] QC STATUS TABLE")
        self.qcTable(self.sensorStatusList)

    # Command QY - checking all I2C sensors
    def command_qy(self):
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

    # Command QX - checking all UART ports
    def command_qx(self):
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

    # Command QW - checking all SAMD sensors
    def command_qw(self):
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

    # Command QV - checking things related to GSM
    def command_qv(self):
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

    def mapRange(self, value, inMin, inMax, outMin, outMax):
        return outMin + (((value - inMin) / (inMax - inMin)) * (outMax - outMin))

    def command_qe(self):
        try:
            self.logger_console.info("Checking 4-20 module")
            gpio.select_I2C(0)
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

    def command_qd(self):
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

    # if sensor type is alphasense then Part No is 101
    # elif sensor type is semeeatech then part no is 104
    # elif sensor type is Nevada Nano then part no is 106 also baud is 38400
    def command_qt(self):
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
            for i in range(0, 1):
                self.sensor.getSensorReading()
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

    # Command QU - checking things related to System
    def command_jj(self):
        rprint("[yellow] HIDDEN COMMANDS TABLE")
        self.logger_file.info("[yellow] HIDDEN COMMANDS TABLE")
        self.generate_table(self.rdCommands)
        return

    def sendDataToSocket(self, payload):
        r = requests.post(OIZOM_SOCKET + QC_API, json=payload, headers=self.headers)
        rprint(r.status_code, r.reason)
        rprint(r.text)

    # DNS table
    def qcTable(self, dictt):
        """Make a new table."""
        table = Table(style="cyan", highlight=True)
        table.add_column("Sr. No.", style="dim", width=7, justify="center")
        table.add_column("Command", width=15, justify="left")
        table.add_column("Status", width=10, justify="center")
        console = Console()
        i = 0
        for key, value in dictt.items():
            key = [key for key in dictt.keys()][i]
            value = [value for value in dictt.values()][i]
            key, value = str(key + " " + "test"), str(bool(value))
            table.add_row(str(i), key, value)
            i += 1
        console.print(table)
        self.printTABLE_file(table)
        return table

    # Generate table from the given Dictionary
    def generate_table(self, dictt):
        """Make a new table."""
        table = Table(style="cyan", highlight=True)
        table.add_column("Sr. No.", style="dim", width=7, justify="center")
        table.add_column("Command", justify="center")
        table.add_column("Description", justify="left")
        table.add_column("Status", justify="center")
        console = Console()
        i = 0
        for key, value in dictt.items():
            key = [key for key in dictt.keys()][i]
            value = [value for value in dictt.values()][i]
            key, value = str(key), str(value)
            table.add_row(str(i), key, value)
            i += 1
        console.print(table)
        self.printTABLE_file(table)
        return table

    ## MBBS = MotherBoard Biography Started
    def comm_a(self):
        rprint("[green] MB testing started")
        self.logger_file.info("[green] MB testing started")
        rprint(
            "[yellow] ++ Includes LED test, SHT, BME, Battery/MPPT, Light-UV, Fan test"
        )
        self.logger_file.info(
            "[yellow] ++ Includes LED test, SHT, BME, Battery/MPPT, Light-UV, Fan test"
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

    ## Helium MBBS = MotherBoard Biography Started
    def comm_ha(self):
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
        self.helium_sensorList.pop("batt", None)
        self.helium_sensorList.pop("noise", None)
        self.helium_sensorList.pop("wind", None)
        self.helium_sensorList.pop("rain", None)
        self.qcTable(self.helium_sensorList)
        self.mbFlag = True
        rprint("QC MB Flag - ", self.mbFlag)
        self.logger_file.info(f"QC MB Flag - {self.mbFlag}")
        return self.mbFlag

    ## DNS Testing
    def comm_b(self):
        rprint("[green] DNS testing started")
        self.logger_file.info("[green] DNS testing started")
        rprint(
            "[yellow] ++ Includes LED test, SHT, BME, Battery/MPPT, Light-UV, Fan test"
        )
        self.logger_file.info(
            "[yellow] ++ Includes LED test, SHT, BME, Battery/MPPT, Light-UV, Fan test"
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

    ## Helium DNS Testing
    def comm_hb(self):
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

    ## HOLO ALLOCATION
    def comm_c(self):
        rprint("[green] HOLO testing started")
        self.logger_file.info("[green] HOLO testing started")
        rprint(
            "[yellow] ++ Includes LED test, SHT, BME, Battery/MPPT, Light-UV, Fan test"
        )
        self.logger_file.info(
            "[yellow] ++ Includes LED test, SHT, BME, Battery/MPPT, Light-UV, Fan test"
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

    ## HOLO ALLOCATION
    def comm_hc(self):
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

    def comm_e5(self):
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

    # printing the Hydroxy Logo
    def printLOGO(self):
        f = open("QC/logo.txt")
        content = f.read()
        rprint(f"[white] {content}")  # can change the logo color from here
        # self.logger_file.info(content)                    # To print logo in app.log file

    def printTABLE_file(self, table):
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

    def millis(self):
        return int(time.time() * 1000)

    # asking for the input - new method
    def get_input(self, prompt):
        try:
            command = input(prompt)
        except EOFError as e:
            rprint(e)
            self.logger_file.exception(e)
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

    def parse_arguments(self, args):
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
    qc.printLOGO()
    qc.command_q()

    while True:
        try:
            answer = qc.get_input("Enter Command:> ")
            if answer == "x":
                break
        except Exception as e:
            qc.logger_console.exception(e)
    qc.logger_console.info("---------- QC file ends ----------")