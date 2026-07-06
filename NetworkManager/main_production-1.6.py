from __future__ import print_function
from __future__ import absolute_import
import netifaces as ni
import time
import logging
import os
from threading import Thread
import subprocess
import json
import socket
import re
import shutil
import psutil
import ipaddress

from flask import Flask,Response,send_from_directory
from flask import request
from flask import json
from flask_cors import CORS
import platform

HOSTNAME_FILE ="/oizom/hostname"
APN_FILE = "/oizom/apn.json"
UPLOAD_FOLDER = './bossac/'

app = Flask(__name__,static_folder='build')
CORS(app)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)

class AccessPoint:
    config = '''#sets the wifi interface to use, is wlan0 in most cases
interface={2}
#driver to use, nl80211 works in most cases
driver=nl80211
#sets the ssid of the virtual wifi access point
ssid={0}
#sets the mode of wifi, depends upon the devices you will be using. It can be a,b,g,n. Setting to g ensures backward compatiblity.
hw_mode=g
#sets the channel for your wifi
channel=6
#macaddr_acl sets options for mac address filtering. 0 means "accept unless in deny list"
macaddr_acl=0
#setting ignore_broadcast_ssid to 1 will disable the broadcasting of ssid
ignore_broadcast_ssid=0
#Sets authentication algorithm
#1 - only open system authentication
#2 - both open system authentication and shared key authentication
auth_algs=1
#####Sets WPA and WPA2 authentication#####
#wpa option sets which wpa implementation to use
#1 - wpa only
#2 - wpa2 only
#3 - both
wpa=2
#sets wpa passphrase required by the clients to authenticate themselves on the network
wpa_passphrase={1}
#sets wpa key management
wpa_key_mgmt=WPA-PSK
#sets encryption used by WPA
#wpa_pairwise=TKIP
#sets encryption used by WPA2
rsn_pairwise=CCMP
#################################
#####Sets WEP authentication#####
#WEP is not recommended as it can be easily broken into
#wep_default_key=0
#wep_key0=qwert    #5,13, or 16 characters
#optionally you may also define wep_key2, wep_key3, and wep_key4
#################################
#For No encryption, you don't need to set any options'''

    def __init__(self, wlan='hotspot', inet=None, ip='192.168.45.1', netmask='255.255.255.0', ssid='MyAccessPoint',
                 password='1234567890'):
        self.wlan = wlan
        self.inet = inet
        self.ip = ip
        self.netmask = netmask
        self.ssid = ssid
        self.password = password
        self.root_directory = "/etc/accesspoint/"
        self.hostapd_config_path = os.path.join(self.root_directory, "hostapd.config")

        if not os.path.exists(self.root_directory):
            os.makedirs(self.root_directory)

    def _check_parameters(self):
        interfaces = ni.interfaces()

        if self.wlan not in interfaces:
            logging.error("Wlan {} interface was not found".format(self.wlan))
            return False

        if self.inet is not None and self.inet not in interfaces:
            logging.error("Inet {} interface was not found".format(self.inet))
            return False

        if not self._validate_ip(self.ip):
            logging.error("Wrong ip {}".format(self.ip))
            return False

        if self.ssid is None:
            logging.error("SSID must not be None")
            return False

        self.ssid = str(self.ssid)

        if self.password is None:
            logging.error("Password must not be None")
            return False

        self.password = str(self.password)

        return True

    def _write_hostapd_config(self):
        with open(self.hostapd_config_path, 'w') as hostapd_config_file:
            hostapd_config_file.write(self.config.format(self.ssid, self.password, self.wlan))

        logging.debug("Hostapd config saved to %s", self.hostapd_config_path)

    def _validate_ip(self, addr):
        try:
            socket.inet_aton(addr)
            return True  # legal
        except socket.error:
            logging.error("Wrong ip %s", str(addr))
            return False  # Not legal

    def _check_dependencies(self):
        check = True

        if shutil.which('hostapd') is None:
            logging.error('hostapd executable not found. Make sure you have installed hostapd.')
            check = False

        if shutil.which('dnsmasq') is None:
            logging.error('dnsmasq executable not found. Make sure you have installed dnsmasq.')
            check = False

        return check

    def _pre_start(self):
        try:
            self._execute_shell('rfkill unblock wlan')
            self._execute_shell('sleep 1')
        except:
            pass

    def _start_router(self):
        self._pre_start()
        s = 'ifconfig ' + self.wlan + ' up ' + self.ip + ' netmask ' + self.netmask
        logging.debug('created interface: mon.' + self.wlan + ' on IP: ' + self.ip)
        r = self._execute_shell(s)
        logging.debug(r)
        # print('sleeping for 2 seconds.')
        logging.debug('wait..')
        self._execute_shell('sleep 2')
        i = self.ip.rindex('.')
        ipparts = self.ip[0:i]

        # enable forwarding in sysctl.
        logging.debug('enabling forward in sysctl.')
        r = self._execute_shell('sysctl -w net.ipv4.ip_forward=1')
        logging.debug(r.strip())

        if self.inet is not None:
            # enable forwarding in iptables.
            logging.debug('creating NAT using iptables: {} <-> {}'.format(self.wlan, self.inet))
            self._execute_shell('iptables -P FORWARD ACCEPT')

            self._execute_shell('iptables -t nat -D POSTROUTING -o {} -j MASQUERADE'.format(self.inet))
            self._execute_shell('iptables -D FORWARD -i {} -o {} -j ACCEPT -m state --state RELATED,ESTABLISHED'.format(self.inet, self.wlan))
            self._execute_shell('iptables -D FORWARD -i {} -o {} -j ACCEPT'.format(self.wlan, self.inet))

            self._execute_shell('iptables -t nat -A POSTROUTING -o {} -j MASQUERADE'.format(self.inet))
            self._execute_shell(
                'iptables -A FORWARD -i {} -o {} -j ACCEPT -m state --state RELATED,ESTABLISHED'
                    .format(self.inet, self.wlan))
            self._execute_shell('iptables -A FORWARD -i {} -o {} -j ACCEPT'.format(self.wlan, self.inet))

        # allow traffic to/from wlan
        self._execute_shell('iptables -A OUTPUT --out-interface {} -j ACCEPT'.format(self.wlan))
        self._execute_shell('iptables -A INPUT --in-interface {} -j ACCEPT'.format(self.wlan))

        # start dnsmasq
        s = 'dnsmasq --dhcp-authoritative --interface={} --dhcp-range={}.20,{}.100,{},4h'\
            .format(self.wlan, ipparts, ipparts, self.netmask)

        logging.debug('running dnsmasq')
        logging.debug(s)
        r = self._execute_shell(s)
        logging.debug(r)

        s = 'hostapd -B {}'.format(self.hostapd_config_path)
        logging.debug(s)
        logging.debug('running hostapd')
        # print('sleeping for 2 seconds.')
        logging.debug('wait..')
        self._execute_shell('sleep 2')
        r = self._execute_shell(s)
        logging.debug(r)
        logging.debug('hotspot is running.')
        return True

    def _stop_router(self):
        # bring down the interface
        # self._execute_shell('ifconfig mon.' + self.wlan + ' down')

        # stop hostapd
        logging.debug('stopping hostapd')
        self._execute_shell('pkill hostapd')

        # stop dnsmasq
        logging.debug('stopping dnsmasq')
        self._execute_shell('killall dnsmasq')

        logging.debug('hotspot has stopped.')
        return True

    def is_running(self):
        proceses = [proc.name() for proc in psutil.process_iter(attrs=['name', 'status']) if proc.info['status'] != psutil.STATUS_ZOMBIE]
        return 'hostapd' in proceses or 'dnsmasq' in proceses

    def stop(self):
        if not self._check_parameters():
            return False

        if not self.is_running():
            logging.debug("Not running")
            return True

        return self._stop_router()

    def start(self):
        if not self._check_dependencies():
            return False

        if not self._check_parameters():
            return False

        if self.is_running():
            logging.debug("Already started")
            return True

        self._write_hostapd_config()

        return self._start_router()

    def _execute_shell(self, command_string):
        p = subprocess.Popen(command_string, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.wait()
        result = p.communicate()

        return result[0].decode()

class PiCommander(object):
    def __init__(self, **kwargs):
        self.sudo_path = '/usr/bin/sudo'
        
        allowed = ['sudo_path']
        for k, v in kwargs.items():
            if k in allowed:
                setattr(self, k, v)


    def run_command(self, commands, sudo=False):
        if sudo:
            commands = [self.sudo_path] + commands
    
        child = subprocess.Popen(
            commands, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE)
    
        output, errors = child.communicate()
        return (child.returncode, output, errors) 
    
class WpaManager(PiCommander):
    def __init__(self, **kwargs):
        super(WpaManager, self).__init__(**kwargs)
        self.sudo = False
        self.wpa_cli = '/sbin/wpa_cli'
        self.wpa_passphrase = '/sbin/wpa_passphrase'
        self.wifi_interface = 'wlan0'
        
        allowed = ['sudo', 'wpa_cli', 'wpa_passphrase']
        for k, v in kwargs.items():
            if k in allowed:
                setattr(self, k, v)
    
    
    """
    list networks in wpa_supplicant
    
    params:
        None
    returns:
        a list: [[index, ssid, bsid, flags], [etc...] ]
    """
    def list_networks(self):
        mylist = []
        cmd = [self.wpa_cli, '-i', self.wifi_interface, 'list_networks']
        rval, out, err = self.run_command(cmd, self.sudo)
        for line in out.decode('utf-8').splitlines():            
            if not re.search(r'^\d+', line):
                # skip lines not starting with an index number
                continue
    
            id, ssid, bsid, flags = line.split("\t")
            if bsid:
                bsid = bsid.strip()
                
            if flags:
                flags = flags.strip()
                
            mylist.append([int(id.strip()), ssid.strip(), bsid, flags])
            
        return mylist
        
    
    """
    add new network to the wpa_supplicant.
    dict keys and values should match exactly the wpa_supplicant network
    fields you want to add.
    
    params:
        a dict containing wpa network settings
        { 'ssid': 'Foo', 'psk': "mypassword", 'scan_ssid': '1' }
    returns:
        0 on success, non-zero on failure
    """           
    def add_network(self, n):
        failures = 0
        idx = self.new_network_index()
        if idx is None:
            # really, is this the best I could do?
            # let's put a proper Exception here at some point, ok?
            raise
                    
        for k, v in n.items():    
            if self.set_network(idx, k, v):
                failures += 1
                # oops! something went wrong. don't leave partial networks
                # in the system. Delete it!
                # Exceptions, or error msgs, would be good here.
                failures += self.remove_network(idx)
                break    
        
        if not failures:
            if self.enable_network(idx):
                failures += 1
            else:
                if self.save_config():
                    failures += 1
    
        if not failures:
            if self.select_network(idx):
                failures += 1
            else:
                if self.save_config():
                    failures += 1
    
        return [failures, idx]

    """
    Set network parameters
    params:
        - network index
        - parameter
        - setting
    returns:
        exit-code (0 = success)
    """
    def set_network(self, idx, key, val):
        quoted = ['ssid', 'psk', 'identity', 'ca_cert', 'client_cert',
                    'private_key', 'private_key_passwd', 'password',
                    'anonymous_identity']
                    
        if key in quoted:
            val = '"%s"' % str(val)
        else:
            val = str(val)  
            
        cmd = [self.wpa_cli, '-i', self.wifi_interface, 'set_network', str(idx), key, val]
        rval, out, err = self.run_command(cmd, self.sudo)
        return rval


    """
    Remove a network from wpa_supplicant
    params:
        - network index number
    returns:
        exit-code (0 = success)
    """
    def remove_network(self, idx):
        cmd = [self.wpa_cli, '-i', self.wifi_interface, 'remove_network', str(idx)]
        rval, out, err = self.run_command(cmd, self.sudo)
        return rval


    """
    write wpa_supplicant network info to the config file
    params:
        None
    returns:
        0 on success, non-zero on failure
    """
    def save_config(self):
        cmd = [self.wpa_cli, '-i', self.wifi_interface, 'save_config']
        rval, out, err = self.run_command(cmd, self.sudo)
        return rval
        
    
    """
    enable a network
    
    params:
        - a network index number
    returns:
        0 on success, non-zero on failure
    """           
    def enable_network(self, idx):
        cmd = [self.wpa_cli, '-i', self.wifi_interface, "enable_network", str(idx)]
        rval, out, err = self.run_command(cmd, self.sudo)
        return rval
        
    def select_network(self, idx):
        cmd = [self.wpa_cli, '-i', self.wifi_interface, "select_network", str(idx)]
        rval, out, err = self.run_command(cmd, self.sudo)
        return rval
        
          
    """
    get a passphrase from wpa_passphrase, based on a string password
    """
    def passphrase(self, passwd):
        cmd = [self.wpa_passphrase, 'Foo', passwd]
        rval, out, err = self.run_command(cmd, self.sudo)
        for line in out.decode('utf-8').splitlines():
            if re.search(r'^\s*(#|$)', line):
                continue
            
            match = re.search(r'psk=(.+)', line)
            if match:
                return match.group(1)
    
        return None
    
    
    """
    Get new index number for network to be added
    """
    def new_network_index(self):
        cmd = [self.wpa_cli, '-i', self.wifi_interface, "add_network"] 
        rval, out, err = self.run_command(cmd, self.sudo)
        for line in out.decode('utf-8').splitlines():
            match = re.search(r'^\s*(\d+)\s*$', line)
            if match:
                return int(match.group(1))
    
        return None

class EthernetManager:
    def __init__(self, interface_name='eth0',dhcpcd_conf='/oizom/dhcpcd.conf'):
        self.dhcpcd_conf = dhcpcd_conf
        self.interface_name = interface_name

    def _parse_saved_static_settings(self, data, eth_index):
        saved_settings = {
            'addr': '',
            'netmask': '',
            'gateway': '',
            'dns': ['']
        }

        try:
            for offset in range(1, 4):
                if eth_index + offset >= len(data):
                    continue

                line = data[eth_index + offset].lstrip('#').strip()

                if line.startswith('static ip_address='):
                    value = line.split('=', 1)[1].strip()
                    if '/' in value:
                        ip_address, prefix = value.split('/', 1)
                        saved_settings['addr'] = ip_address.strip()
                        try:
                            saved_settings['netmask'] = str(ipaddress.IPv4Network(f'0.0.0.0/{prefix}').netmask)
                        except Exception:
                            saved_settings['netmask'] = prefix.strip()
                    else:
                        saved_settings['addr'] = value
                elif line.startswith('static routers='):
                    saved_settings['gateway'] = line.split('=', 1)[1].strip()
                elif line.startswith('static domain_name_servers='):
                    dns_value = line.split('=', 1)[1].strip()
                    saved_settings['dns'] = [dns for dns in dns_value.replace(',', ' ').split() if dns]
        except Exception as e:
            logging.debug(f"Error parsing saved static settings: {e}")

        return saved_settings

    def dhcp(self):
        return self.__change_static_ip(ethernet_interface="dhcp", ip_address=None, routers=None, dns=None, netmask=None)

    def static(self, settings):
        if not all(k in settings for k in ('ip', 'gateway', 'dns', 'netmask')):
            logging.debug("Missing required settings for static IP configuration.")
            return "Missing required settings for static IP configuration."
        return self.__change_static_ip(ethernet_interface="static", ip_address=settings['ip'], routers=settings['gateway'], dns=settings['dns'], netmask=settings['netmask'])

    def __change_static_ip(self, ethernet_interface, ip_address, routers, dns, netmask):
        dhcpd_file = self.dhcpcd_conf
        if ethernet_interface == 'static':
            verify = verifyIP(ip_address, routers, netmask, dns)
            if verify != "Valid":
                logging.debug(f"IP verification error: {verify}")
                return verify

            prefix = ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
            dns_value = " ".join(dns.replace(","," ").strip().split())

            try:            
                # Sanitize/validate params above
                with open(dhcpd_file, 'r') as file:
                    data = file.readlines()

                # Find if config exists
                ethFound = next((x for x in data if f'interface {self.interface_name}' in x), None)

                if ethFound:
                    ethIndex = data.index(ethFound)
                    if data[ethIndex].startswith('#'):
                        interface_default = data[ethIndex].replace("#","") # commented out by default, make active
                        data[ethIndex] = interface_default

                # If config is found, use index to edit the lines you need ( the next 3)
                if ethIndex:
                    data[ethIndex+1] = f'static ip_address={ip_address}/{prefix}\n'
                    data[ethIndex+2] = f'static routers={routers}\n'
                    data[ethIndex+3] = f'static domain_name_servers={dns_value}\n'
                
                if not ethFound or not ethIndex:
                    raise Exception("Ethernet config not found in dhcpcd.conf")
                    
                logging.debug(f"Writing to {dhcpd_file}")
                with open(dhcpd_file, 'w') as file:
                    file.writelines( data )

            except Exception as ex:
                logging.debug(f"IP changing error: {ex}")
                return "Error setting static IP"
        elif ethernet_interface == "dhcp":
            try:            
                # Sanitize/validate params above
                with open(dhcpd_file, 'r') as file:
                    data = file.readlines()
                    
                # Find if config exists
                ethFound = next((x for x in data if f'interface {self.interface_name}' in x), None)

                if ethFound:
                    ethIndex = data.index(ethFound)
                    if not data[ethIndex].startswith('#'):
                        interface_default = f"#{data[ethIndex]}" # commented out by default, make active
                        data[ethIndex] = interface_default

                # If config is found, use index to edit the lines you need ( the next 3)
                if ethIndex:
                    data[ethIndex+1] = f'#{data[ethIndex+1]}'
                    data[ethIndex+2] = f'#{data[ethIndex+2]}'
                    data[ethIndex+3] = f'#{data[ethIndex+3]}'
                
                if not ethFound or not ethIndex:
                    raise Exception("Ethernet config not found in dhcpcd.conf")
                    
                logging.debug(f"Writing to {dhcpd_file}")
                with open(dhcpd_file, 'w') as file:
                    file.writelines( data )

            except Exception as ex:
                logging.debug(f"IP changing error: {ex}")
                return "Error setting DHCP"
        self.restart_ethernet()
        return "Settings saved successfully"
                
    def restart_ethernet(self):
        command = f"ifconfig {self.interface_name} down && ifconfig {self.interface_name} up"
        logging.debug(f"Executing.... {command}")
        self.execute_os_command(command)

    def get_dns(self):
        dns_list = []
        command = f"cat /etc/resolv.conf"
        _dns_settings = self.execute_os_command(command)
        _dns_settings_list = _dns_settings.split('\n')
        for _dns in _dns_settings_list:
            if _dns.startswith('nameserver'):
                dns = _dns.split(' ')[1]
                dns_list.append(dns)
        return dns_list

    def status(self):
        dhcpd_file = self.dhcpcd_conf
        ethernet_mode = "static"
        ethernet_status = {}
        try:            
            # Sanitize/validate params above
            with open(dhcpd_file, 'r') as file:
                data = file.readlines()
            # Find if config exists
            ethFound = next((x for x in data if f'interface {self.interface_name}' in x), None)
            if ethFound:
                ethIndex = data.index(ethFound)
                if data[ethIndex].startswith('#'):
                    ethernet_mode = "dhcp"
                ethernet_status.update({"saved_static": self._parse_saved_static_settings(data, ethIndex)})

        except Exception as e:
            logging.debug(f"[ERR] ETH MODE {e}")
        try:
            ethernet_status.update(ni.ifaddresses(self.interface_name)[ni.AF_INET][0])
            for _interface in ni.gateways()[ni.AF_INET]:
                if _interface[1]  == self.interface_name:
                    ethernet_status.update({'gateway':_interface[0]})
            ethernet_status.update({'internet': check_internet(self.interface_name), 'dns':self.get_dns(), 'mode':ethernet_mode })
        except Exception as eth_err:
            logging.debug(f"Ethernet status:{eth_err}")
            ethernet_status.update({"internet":False})
        return ethernet_status

    def execute_os_command(self,command):
        p = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.wait()
        result = p.communicate()
        return result[0].decode()


class WiFiManager:
    def __init__(self, apinterace="hotspot", wifi_interface="wlan0", apssid="OIZOM", appassword="12345678", aphotspot_ip="192.168.45.1", wifi_init_delay=50):
        self.ap_interface = apinterace
        self.wifi_interface = wifi_interface
        self.ap_ssid = apssid
        self.ap_password = appassword
        self.hotspot_ip = aphotspot_ip
        self.status_responses = ['wpa_state', 'ip_address', 'p2p_device_address', 'address' , 'uuid', 'ssid', 'RSSI']
        self.standard_wifi_delay = wifi_init_delay
        self.wifi_enable = True
        self.access_point = AccessPoint(wlan=self.ap_interface, ssid=self.ap_ssid, password=self.ap_password)

    def setup(self):
        interfaces = ni.interfaces()
        if self.wifi_interface in interfaces:
            self.wifi_enable = True
            if "wlan1" in interfaces:
                self.wifi_interface = "wlan1"
            logging.debug(f"WiFi interface Found:{self.wifi_interface}")
        else:
            logging.debug(f"No Interface Found:{self.wifi_interface}")
            self.wifi_enable = False
            return
        self.wpa_manager = WpaManager(sudo=False)
        self.wpa_manager.wifi_interface = self.wifi_interface
        logging.basicConfig(format="%(asctime)s ::%(levelname)s:: %(message)s",
                            level=logging.DEBUG)
        command = "wpa_supplicant -B -Dnl80211 -iwlan0 -c /etc/wpa_supplicant/wpa_supplicant.conf"
        if self.wifi_interface == "wlan1":
            command = "wpa_supplicant -B -Dwext -iwlan1 -c /etc/wpa_supplicant/wpa_supplicant.conf"
        # start wpa_supplicant
        Thread(target=os.system, args=(command,), daemon=True).start()
        # This delay is very important unless wpa won't connect
        logging.debug(f"Delay of {self.standard_wifi_delay}")
        time.sleep(self.standard_wifi_delay)
        connection_status = self.connection_status()
        logging.debug(f"Connection status: {connection_status}")
        self.wpa_manager.wpa_cli = "wpa_cli"
        self.wpa_passphrase = "wpa_passphrase"
        if connection_status != "COMPLETED" or self.wifi_interface == "wlan1":
            self.create_hotspot()
      
    def create_hotspot(self):
        logging.debug(f"Creating Hotspot SSID:{self.ap_ssid} PSK: {self.ap_password}")

        if self.access_point.is_running():
            logging.debug("Hotspot is already running. Skipping restart.")
            return

        logging.debug("Removing and configuring AP interface...")
        self.remove_ap_interace(self.ap_interface)
        self.check_ap_interface(self.ap_interface)
        self.add_ap_interace(self.ap_interface)
        self.up_ap_interface(self.ap_interface)
        self.configure_ap_interace(self.ap_interface, self.hotspot_ip)

        try:
            self.access_point.stop()
            time.sleep(2)
        except Exception as e:
            logging.debug(f"[WARN] Error stopping hotspot (might not be running): {e}")

        logging.debug("Starting hotspot...")
        self.access_point.start()

    def stop_hotspot(self):
        try:
            self.access_point.stop()
            self.remove_ap_interace(self.ap_interface)
        except Exception as e:
            logging.debug(f"[ERR] STOP_HOTSPOT:{e}")

    def add_ap_interace(self,interface):
        command = f'iw phy phy0 interface add {interface} type __ap'
        logging.debug(f"os: {command}")
        response = self.execute_os_command(command)
        logging.debug(response)

    def remove_ap_interace(self,interface):
        command = f"iw dev {interface} del"
        logging.debug(f"os: {command}")
        response = self.execute_os_command(command)
        logging.debug(response)

    def configure_ap_interace(self,interface, hotspot_ip):
        command = f"ifconfig {interface} {hotspot_ip}"
        logging.debug(f"os: {command}")
        response = self.execute_os_command(command)
        logging.debug(response)
        
    def check_ap_interface(self,interface):
        command = f"ifconfig {interface}"
        logging.debug(f"os: {command}")
        response = self.execute_os_command(command)
        logging.debug(response)
        return True

    def up_ap_interface(self,interface):
        command = f"ifconfig {interface} up"
        response = self.execute_os_command(command)
        logging.debug(response)

    def execute_os_command(self,command):
        p = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.wait()
        result = p.communicate()
        return result[0].decode()

        # status = subprocess.check_output(command, shell=True)
        # status = status.decode()
        # return status

    def scan(self):
        try:
            logging.debug("SCANNING WIFI")
            subprocess.run(["wpa_cli", "-i", self.wifi_interface, "scan"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            result = subprocess.run(["wpa_cli", "-i", self.wifi_interface, "scan_results"], capture_output=True, text=True)
            lines = result.stdout.splitlines()
            networks = []
            for idx, line in enumerate(lines[2:], start=1):
                parts = line.split("\t")
                if len(parts) >= 5:
                    bssid, freq, signal, flags, ssid = parts[:5]
                    encryption = "off" if "[ESS]" in flags and "WPA" not in flags else "on"
                    networks.append({
                        "cellnumber": f"{idx:02}",
                        "mac": bssid.upper(),
                        "frequency": str(round(int(freq)/1000, 3)),
                        "signal_quality": "",  # Placeholder if needed
                        "signal_total": "70",
                        "signal_level_dBm": signal,
                        "encryption": encryption,
                        "essid": ssid,
                        "mode": "Master",
                        "ieee": ""  # Could not be fetched from scan_results
                    })
            return networks

        except Exception as e:
            logging.error(f"WiFi scan failed: {e}")
            return []

    def list_history(self):
        return self.wpa_manager.list_networks()


    def connect_wifi(self,ssid, password):
        logging.debug(self.wpa_manager.list_networks())
        logging.debug(f"Connecting to {ssid} {password}")
        if (len(password) > 0):
            failures, idx = self.wpa_manager.add_network({'ssid':ssid,'scan_ssid':1,'psk':password})
        else:
            failures, idx = self.wpa_manager.add_network({'ssid':ssid,'scan_ssid':1,'key_mgmt':'NONE'})
        logging.debug(f"failures: {failures}, idx: {idx}")
        self.wpa_manager.enable_network(idx)
        self.wpa_manager.save_config()
        if failures != 0:
            return "Failed to connect to WiFi network"
        return "Settings saved successfully"

    def connection_status(self):
        response = self.status()
        return response['wpa_state']
    
    def status(self, scan_flag = False):
        # internet flag
        response = {}
        try:
            status = subprocess.check_output(f"wpa_cli -i {self.wifi_interface} status", shell=True)
            status = status.decode()
            _status_list = status.split('\n')
            signal_poll = subprocess.check_output(f"wpa_cli -i {self.wifi_interface} signal_poll", shell=True)
            signal_poll = signal_poll.decode()
            _signal_poll_list = signal_poll.split('\n')
            _status_list.extend(_signal_poll_list)
            for _s in _status_list:
                _parse = _s.split('=')
                if _parse[0] in self.status_responses:
                    if _parse[0] == "wpa_state" and _parse[1] != "COMPLETED":
                        if scan_flag:
                            response.update({"ssid_list":self.scan()})
                    response.update({_parse[0]:_parse[1]})
        
        
            if response["wpa_state"] != "COMPLETED":
                response.update({"internet":False})
            else:
                if "ip_address" in response:
                    response.update({"internet": check_internet(self.wifi_interface)})
                else:
                    response.update({"wpa_state":"NOTCOMPLETED"})
                    response.update({"internet":False})
        except Exception as wifi_err:
            logging.debug(f"WiFi status:{wifi_err}")
            response.update({"internet":False})

        return response
    

    def delete_network(self,wifi_id):
        self.wpa_manager.remove_network(wifi_id)
        self.wpa_manager.save_config()
    
    def reconnect(self,wifi_id):
        command = f"wpa_cli -i {self.wifi_interface} select_network {wifi_id}"
        reconnection_result = self.execute_os_command(command)
        logging.debug(f"Reconnecting to... {reconnection_result}")
        self.wpa_manager.save_config()

    def hotspot_status(self):
        response = {
            "ssid": self.ap_ssid,
            "password": self.ap_password,
            "ip": self.hotspot_ip,
            "status": self.access_point.is_running()
        }
        
        return response

class GsmManager:
    def __init__(self):
        interfaces = ni.interfaces()
        self.interface_name = None
        if 'usb0' in interfaces:
            self.interface_name = 'usb0'
        elif 'wwan0' in interfaces:
            self.interface_name = 'wwan0'

    def status(self):
        gsm_status = {}
        if not self.interface_name:
            logging.debug("No GSM interface found")
            gsm_status.update({"message":"No modem found"})
        else:
            try:
                gsm_status = ni.ifaddresses(self.interface_name)[ni.AF_INET][0]
                for _interface in ni.gateways()[ni.AF_INET]:
                    if _interface[1]  == self.interface_name:
                        gsm_status.update({'gateway':_interface[0]})
                gsm_status.update({'internet': check_internet(self.interface_name), 'dns':self.get_dns()})
            except Exception as gsm_err:
                logging.debug(f"[ERR]GSM status:{gsm_err}")
                gsm_status.update({"internet":False})
        try:
            with open(APN_FILE, 'r+') as apn_file:
                apn_list = json.load(apn_file)
                gsm_status.update({"apn":apn_list['user']})
        except Exception as apn_err:
            logging.debug(f"[APN] err status:{apn_err}")
        return gsm_status
    def get_dns(self):
        dns_list = []
        command = f"cat /etc/resolv.conf"
        _dns_settings = self.execute_os_command(command)
        _dns_settings_list = _dns_settings.split('\n')
        for _dns in _dns_settings_list:
            if _dns.startswith('nameserver'):
                dns = _dns.split(' ')[1]
                dns_list.append(dns)
        return dns_list
    def execute_os_command(self,command):
        p = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.wait()
        result = p.communicate()
        return result[0].decode()

    def add_apn(self,en,apn):
        try:
            with open(APN_FILE, 'r+') as apn_file:
                apn_list = json.load(apn_file)
                apn_list['user'] = {
                    "en":en,
                    "apn":apn
                }
                apn_file.seek(0)
                json.dump(apn_list, apn_file, indent=4)
                apn_file.truncate()
        except Exception as apn_err:
            logging.debug(f"[APN] err status:{apn_err}")
            return "Error saving APN"
        return "APN saved successfully"

class Samd:
    def __init__(self):
        pass

    def updateSamdFirmware(self, firmware_file, port = "ttyACM0"):
        arc = platform.machine()
        if arc == "x86_64":
            arc = "x86_64"
        if arc == "armv7l":
            arc = "arm"
        command = f"bash -x ./bossac/upload.sh {port} {firmware_file} {arc}"
        logging.debug(command)
        yield (command+'\n')
        proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
                )
        for c in iter(lambda: proc.stdout.readline(), b''): 
            # logging.debug(c)
            yield(c)
        yield("Uploaded Successfully\n")

def check_internet(interface, host="8.8.8.8", port=53, timeout=5):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode())
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception as e:
        logging.debug(f"check_internet {interface} socket error: {e}")
        return False

def gethostname():
    with open(HOSTNAME_FILE, "r+") as f:
        hostname = f.read()
        logging.debug(f"hostname: {hostname}")
        hostname = hostname.rstrip()
        hostname = hostname.lstrip()
        return hostname

def verifyIP(ip_address, routers, netmask, dns):
    try:
        prefix = ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
    except Exception as ex:
        return "Invalid Netmask"
    try:
        dns_value = " ".join(dns.replace(","," ").strip().split())
        for _dns in dns_value.split(" "):
            ipaddress.IPv4Address(_dns)
    except Exception as ex:
        return "Invalid DNS"
    try:
        ipaddress.IPv4Address(ip_address)
    except Exception as ex:
        return "Invalid IP Address"
    try:
        ipaddress.IPv4Address(routers)
    except Exception as ex:
        return "Invalid Gateway"
    return "Valid"

wifimanager = WiFiManager(apssid=gethostname())
wifimanager.setup()
ethernetmanager = EthernetManager()
gsmmanager = GsmManager()
samd = Samd()
logging.debug("Running setup")

# Serve React App
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    if path != "" and os.path.exists(app.static_folder + '/' + path):
        return send_from_directory(app.static_folder, path)
    else:
        return send_from_directory(app.static_folder, 'index.html')

def getAllstatus():
    wifi_status = {
        "internet":False,
        "status":"DISABLED"
    }
    if wifimanager.wifi_enable == True:
        wifi_status = wifimanager.status(True)
    eth_status = ethernetmanager.status()
    gsm_status = gsmmanager.status()
    hotspot_status = wifimanager.hotspot_status()
    return { 'wifi':wifi_status, 'eth': eth_status, 'gsm': gsm_status, 'hotspot': hotspot_status }

@app.route('/network/wifi/scan', methods=['GET'])
def scanner():
    scan_result = wifimanager.scan()
    response = app.response_class(
        response=json.dumps(scan_result),
        status=200,
        mimetype='application/json'
    )
    return response

@app.route('/network/wifi/status', methods=['GET'])
def status():
    status_result = wifimanager.status(True)
    response = app.response_class(
        response=json.dumps({
            "message":status_result
        }),
        status=200,
        mimetype='application/json'
    )
    return response

@app.route('/network/wifi/history', methods=['GET'])
def history():
    history_result = wifimanager.list_history()
    logging.debug(f"history_result {history_result}")
    response = app.response_class(
        response=json.dumps({
            "message":history_result
        }),
        status=200,
        mimetype='application/json'
    )
    return response

@app.route('/network/wifi/connect', methods=['POST'])
def connect():
    wifi_settings = request.json
    ssid = wifi_settings['ssid']
    password = wifi_settings['password']
    message = wifimanager.connect_wifi(ssid, password)

    status_response = getAllstatus()
    status_response["message"] = message
    time.sleep(10)
    response = app.response_class(
        response=json.dumps(status_response),
        status=200,
        mimetype='application/json'
    )
    return response

@app.route('/network/wifi/reconnect/<int:wifi_id>', methods=['PUT'])
def reconnect(wifi_id):
    logging.debug(f"Reconnecting {wifi_id}")
    wifimanager.reconnect(wifi_id)
    time.sleep(5)
    status_result = wifimanager.status()
    response = app.response_class(
        response=json.dumps({
            "message": status_result
        }),
        status=200,
        mimetype='application/json'
    )
    return response

@app.route('/network/wifi/delete_wifi/<int:wifi_id>', methods=['DELETE'])
def delete_wifi(wifi_id):
    logging.debug(f"deleting wifi {wifi_id}")
    wifimanager.delete_network(wifi_id)
    history_result = wifimanager.list_history()
    response = app.response_class(
        response=json.dumps({
            "message":history_result
        }),
        status=200,
        mimetype='application/json'
    )
    return response
@app.route('/network/eth/status', methods=['GET'])
def ethernet_status():
    ethernet_status = ethernetmanager.status()
    logging.debug(f"Ethernet status: {ethernet_status}")
    response = app.response_class(
        response=json.dumps(ethernet_status),
        status=200,
        mimetype='application/json'
    )
    return response

@app.route('/network/eth/setting', methods=['POST'])
def ethernet_change():
    ethernet_settings = request.json
    ethernet_mode = ethernet_settings['mode']
    if ethernet_mode == "dhcp":
        message = ethernetmanager.dhcp()
    elif ethernet_mode == "static":
        message = ethernetmanager.static(ethernet_settings)

    status_response = getAllstatus()
    status_response["message"] = message
    response = app.response_class(
        response=json.dumps(status_response),
        status=200,
        mimetype='application/json'
    )
    return response

@app.route('/network/gsm/apn/setting', methods=['POST'])
def apn_change():
    gsm_settings = request.json
    apn = ""
    en = gsm_settings['en']
    if en == 1:
        apn = gsm_settings['apn']
    message = gsmmanager.add_apn(en, apn)
    
    status_response = getAllstatus()
    status_response["message"] = message
    response = app.response_class(
        response=json.dumps(status_response),
        status=200,
        mimetype='application/json'
    )
    return response

@app.route('/network/wifi/createap', methods=['GET'])
def createAP():
    status_result = wifimanager.create_hotspot()
    response = app.response_class(
        response=json.dumps({
            "message":status_result
        }),
        status=200,
        mimetype='application/json'
    )
    return response

@app.route('/network/wifi/stopap', methods=['GET'])
def stopAP():
    status_result = wifimanager.stop_hotspot()
    response = app.response_class(
        response=json.dumps({
            "message":status_result
        }),
        status=200,
        mimetype='application/json'
    )
    return response

@app.route('/network/hotspot/status', methods=['GET'])
def hotspot_status():
    status_result = wifimanager.hotspot_status()
    response = app.response_class(
        response=json.dumps({
            "message":status_result
        }),
        status=200,
        mimetype='application/json'
    )
    return response

'''
Status Sample response
{
    "eth": {
        "addr": "192.168.31.100",
        "broadcast": "192.168.31.255",
        "dns": [
            "8.8.8.8",
            "1.1.1.1",
            "112.110.249.193"
        ],
        "gateway": "192.168.31.1",
        "internet": true,
        "mode": "static",
        "netmask": "255.255.255.0"
    },
    "gsm": {
        "addr": "10.200.243.236",
        "dns": [
            "8.8.8.8",
            "1.1.1.1",
            "112.110.249.193"
        ],
        "gateway": "192.168.31.1",
        "internet": true,
        "netmask": "255.255.255.248",
        "peer": "10.200.243.236"
    },
    "wifi": {
        "address": "dc:a6:32:fe:7d:f4",
        "ip_address": "192.168.31.100",
        "ssid": "<SSID>",
        "uuid": "0c02ef4f-5916-5bfb-9346-b803f782c855",
        "wpa_state": "COMPLETED"
    }
}
'''
@app.route('/network/status', methods=['GET'])
def network_status():
    response = app.response_class(
        response=json.dumps(getAllstatus()),
        status=200,
        mimetype='application/json'
    )
    return response


@app.route('/samd/firmware/update', methods = ['POST'])
def samd_update():
    if 'firmware_bin' in request.files:
        file = request.files['firmware_bin']
        if file:
            filename = file.filename
            firmware_file = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(firmware_file)
        return Response(samd.updateSamdFirmware(firmware_file), mimetype='text/html')
    else:
        return app.response_class(
            response=json.dumps({"message": "Please Provide Firmware file"}),
            status=200,
            mimetype='application/json'
        )

if __name__ == '__main__':
    logging.debug("Running webserver")
    port = os.getenv('IOTEDGE_PORT', 8084)
    app.run(port=port,host='0.0.0.0')