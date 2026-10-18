#!/usr/bin/env python3
"""
Airtel Fiber Router Device Monitor + Telegram Bot
===================================================
Monitors an Airtel Fiber router (Zyxel-based firmware) for
newly connected WiFi devices and sends Telegram notifications.

It also acts as an interactive Telegram bot — send commands to get
real-time device info, name devices, and more.

Telegram commands:
  /devices   — List all currently connected devices (with names)
  /status    — Same as /devices, plus monitor status
  /name <mac> <name>  — Assign a friendly name to a device MAC
  /names     — List all custom device names you've set
  /refresh   — Force an immediate device poll now
  /help      — Show available commands

Usage:
  python3 router_monitor.py            # Continuous mode (bot + monitor)
  python3 router_monitor.py --once     # Single poll cycle (no bot)

Author: Router Monitor Automation
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "router_monitor_config.json"
STATE_FILE = SCRIPT_DIR / "router_monitor_state.json"
OUI_CACHE_FILE = SCRIPT_DIR / "oui_cache.json"
LOG_FILE = SCRIPT_DIR / "router_monitor.log"

# ---------------------------------------------------------------------------
# SSID definitions — discovered from the router's tab.json files.
# Each band has up to 4 SSID indexes. Index 0 is the primary SSID.
# The station list endpoints follow the pattern:
#   /cgi-bin/wlan_staionInfo_list{idx}.cgi   (2.4 GHz)
#   /cgi-bin/wlan5_staionInfo_list{idx}.cgi  (5 GHz)
# ---------------------------------------------------------------------------
BANDS = [
    {
        "name": "WiFi 2.4GHz",
        "prefix": "wlan",       # → wlan_staionInfo_list.cgi
        "ssid_count": 4,        # SSID indices 0..3
        "ssids": ["Home WiFi", "Home WiFi 2", "SSID-2", "SSID-3"],
    },
    {
        "name": "WiFi 5GHz",
        "prefix": "wlan5",      # → wlan5_staionInfo_list.cgi
        "ssid_count": 4,
        "ssids": ["Home WiFi 5G", "Home WiFi 5G 2", "SSID-2", "SSID-3"],
    },
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg):
    """Write a timestamped log line to stdout and the log file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Config / state / JSON helpers
# ---------------------------------------------------------------------------
def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(cfg):
    """Save the config back to disk (used when adding device names via bot)."""
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
    except Exception as e:
        log(f"  WARN: could not save config: {e}")


def load_json(path, default=None):
    if default is None:
        default = {}
    try:
        if Path(path).exists():
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        log(f"  WARN: could not load {path}: {e}")
    return default


def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"  WARN: could not save {path}: {e}")


# ---------------------------------------------------------------------------
# Router login
# ---------------------------------------------------------------------------
def router_login(session, cfg):
    """
    Log into the router and return (True, base_url) on success.

    The login flow (reverse-engineered from /cgi-bin/login_advance.cgi):
      1. GET the login page to extract the session ID (sid).
      2. Compute  md5( urlencode(password).lower_hex + ":" + sid ).
      3. POST  Loginuser, LoginPasswordValue(hash), submitValue=1, LoginSidValue=sid.
      4. A COOKIE_SESSION_KEY cookie is set on success.
    """
    router = cfg["router"]
    ip = router["ip"]
    scheme = "https" if router.get("use_https") else "http"
    base = f"{scheme}://{ip}"
    username = router["username"]
    password = router["password"]
    login_path = router.get("login_path", "/cgi-bin/login_advance.cgi")

    # Step 1 — fetch login page, extract sid
    resp = session.get(f"{base}{login_path}", timeout=10, verify=False)
    m = re.search(r"sid\s*=\s*'([a-f0-9]+)'", resp.text)
    if not m:
        log("  ERROR: could not extract session ID (sid) from login page")
        return False, base
    sid = m.group(1)

    # Step 2 — compute password hash
    encoded = urllib.parse.quote(password, safe="")
    encoded = re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), encoded)
    hash_input = f"{encoded}:{sid}"
    pwd_hash = hashlib.md5(hash_input.encode()).hexdigest()

    # Step 3 — POST login
    login_data = {
        "Loginuser": username,
        "LoginPassword": password,
        "LoginPasswordValue": pwd_hash,
        "submitValue": "1",
        "LoginSidValue": sid,
    }
    session.headers.update({"Referer": f"{base}{login_path}"})
    resp = session.post(f"{base}{login_path}", data=login_data, timeout=10, verify=False)

    # Step 4 — check success (session cookie present)
    if "COOKIE_SESSION_KEY" in session.cookies:
        log(f"  Logged in to router {ip} (sid={sid})")
        return True, base
    else:
        log("  ERROR: login failed — no session cookie received")
        return False, base


# ---------------------------------------------------------------------------
# Device polling — primary method: EasyMesh AP Details (provides hostnames)
# ---------------------------------------------------------------------------

# Regex for EasyMeshTopology table rows: Device Name, Device IP, Device MAC, Parents MAC, Connection Type, Stations, Details
MESH_TOPO_ROW_RE = re.compile(
    r'<td[^>]*>\s*([^<]*)</td>\s*'      # Device Name
    r'<td[^>]*>\s*([^<]*)</td>\s*'      # Device IP
    r'<td[^>]*>\s*([0-9A-Fa-f:]{17})\s*</td>\s*'  # Device MAC
    r'<td[^>]*>\s*([^<]*)</td>\s*'      # Parents MAC
    r'<td[^>]*>\s*([^<]*)</td>\s*'      # Connection Type
    r'<td[^>]*>\s*(\d+)\s*</td>',       # Stations count
    re.IGNORECASE | re.DOTALL,
)

# Regex for EasyMeshAPDetails table rows: Host Name, Host IP, Host MAC, Connected BSS, Host RSSI
AP_DETAILS_ROW_RE = re.compile(
    r'<td[^>]*>\s*([^<]*)</td>\s*'           # Host Name
    r'<td[^>]*>\s*([0-9.]{7,15})\s*</td>\s*' # Host IP
    r'<td[^>]*>\s*([0-9A-Fa-f:]{17})\s*</td>\s*'  # Host MAC
    r'<td[^>]*>\s*([^<]*)</td>\s*'           # Connected BSS (SSID)
    r'<td[^>]*>\s*(-?\d+)\s*</td>',          # Host RSSI
    re.IGNORECASE | re.DOTALL,
)


def _strip_tags(s):
    """Remove HTML tags and whitespace from a cell value."""
    return re.sub(r'<[^>]+>', '', s).strip()


def poll_devices_easymesh(session, base):
    """
    Primary device-polling method using EasyMesh AP Details.

    Flow:
      1. GET EasyMeshTopology.cgi → extract AP MAC addresses (mesh nodes).
      2. For each AP MAC, GET EasyMeshAPDetails.cgi?APMACDetails=<mac>
         → parse Host Name, Host IP, Host MAC, Connected BSS, Host RSSI.

    Returns dict: { mac: {"hostname": ..., "ip": ..., "mac": ..., "ssid": ..., "rssi": ..., "band": ..., "associated": ...} }
    """
    devices = {}

    # Step 1 — get mesh topology to find all AP MACs
    session.headers.update({"Referer": f"{base}/cgi-bin/tabFW.cgi"})
    try:
        resp = session.get(f"{base}/cgi-bin/EasyMeshTopology.cgi", timeout=10, verify=False)
    except Exception as e:
        log(f"  WARN: failed to fetch EasyMeshTopology: {e}")
        return devices

    if "login_advance" in resp.text:
        log("  WARN: session expired while fetching EasyMeshTopology")
        return devices

    # Parse AP MACs from the topology table
    ap_macs = []
    for m in MESH_TOPO_ROW_RE.finditer(resp.text):
        dev_name, dev_ip, dev_mac, parent_mac, conn_type, stations = m.groups()
        dev_mac = dev_mac.lower().strip()
        if dev_mac:
            ap_macs.append((dev_mac, _strip_tags(dev_name), int(stations)))
            log(f"  EasyMesh AP: {_strip_tags(dev_name)} ({dev_mac}) — {stations} station(s)")

    if not ap_macs:
        log("  No EasyMesh APs found in topology — falling back to station-info method")
        return devices

    # Step 2 — for each AP, fetch connected client details (with hostnames)
    session.headers.update({"Referer": f"{base}/cgi-bin/EasyMeshTopology.cgi"})
    for ap_mac, ap_name, station_count in ap_macs:
        if station_count == 0:
            continue
        try:
            # Build URL directly — using params= would URL-encode the colons in
            # the MAC address, which the router's CGI rejects with HTTP 400.
            resp = session.get(
                f"{base}/cgi-bin/EasyMeshAPDetails.cgi?APMACDetails={ap_mac}",
                timeout=10,
                verify=False,
            )
        except Exception as e:
            log(f"  WARN: failed to fetch EasyMeshAPDetails for {ap_mac}: {e}")
            continue

        if "login_advance" in resp.text:
            log(f"  WARN: session expired while fetching AP details for {ap_mac}")
            continue

        for m in AP_DETAILS_ROW_RE.finditer(resp.text):
            hostname, ip, mac, bss, rssi = m.groups()
            mac = mac.lower().strip()
            hostname = _strip_tags(hostname)
            if not mac:
                continue
            devices[mac] = {
                "mac": mac,
                "hostname": hostname if hostname else "",
                "ip": ip.strip(),
                "ssid": _strip_tags(bss),
                "rssi": int(rssi),
                "band": "WiFi",
                "associated": "yes",
            }
            log(f"  Device: {hostname or '(unnamed)'} ({mac}) IP={ip.strip()} RSSI={rssi}")

    return devices


# ---------------------------------------------------------------------------
# Device polling — fallback method: WLAN station-info lists (MAC only, no hostname)
# ---------------------------------------------------------------------------
ROW_RE = re.compile(
    r'<tr\s+align="center">'
    r'<td[^>]*>\s*(\d+)\s*</td>'               # index
    r'<td[^>]*>\s*([0-9A-Fa-f:]{17})\s*</td>'   # MAC
    r'<td[^>]*>\s*(-?\d+)\s*</td>'              # RSSI
    r'<td[^>]*>\s*(\w+)\s*</td>',               # Associated
    re.IGNORECASE,
)


def poll_devices_legacy(session, base):
    """
    Fallback: query WLAN station-info CGI endpoints (MAC + RSSI only, no hostnames).
    Returns dict: { mac: {...} }
    """
    devices = {}
    session.headers.update({"Referer": f"{base}/cgi-bin/wlan_staionInfo.cgi"})

    for band in BANDS:
        band_name = band["name"]
        prefix = band["prefix"]
        ssid_count = band["ssid_count"]
        ssids = band["ssids"]

        for idx in range(ssid_count):
            suffix = "" if idx == 0 else str(idx)
            url = f"{base}/cgi-bin/{prefix}_staionInfo_list{suffix}.cgi"
            ssid_name = ssids[idx] if idx < len(ssids) else f"SSID-{idx}"

            try:
                resp = session.get(url, timeout=10, verify=False)
            except Exception as e:
                log(f"  WARN: failed to fetch {url}: {e}")
                continue

            if "login_advance" in resp.text:
                log(f"  WARN: session expired while fetching {prefix} SSID{idx}")
                continue

            for m in ROW_RE.finditer(resp.text):
                row_idx, mac, rssi, associated = m.groups()
                mac = mac.lower().strip()
                if mac and associated.lower() == "yes":
                    devices[mac] = {
                        "mac": mac,
                        "hostname": "",
                        "ip": "",
                        "band": band_name,
                        "ssid": ssid_name,
                        "rssi": int(rssi),
                        "associated": associated.lower(),
                    }

    return devices


def poll_devices(session, base):
    """
    Poll for connected devices. Uses EasyMesh AP Details (with hostnames) as the
    primary method, falling back to WLAN station-info lists if EasyMesh is unavailable.
    """
    # Try EasyMesh method first (provides hostnames + IPs)
    devices = poll_devices_easymesh(session, base)

    # If EasyMesh returned nothing, fall back to legacy station-info method
    if not devices:
        log("  EasyMesh returned no devices — trying legacy station-info method")
        devices = poll_devices_legacy(session, base)

    return devices


def fetch_current_devices(cfg):
    """Login + poll in one shot. Returns devices dict or None on failure."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    ok, base = router_login(session, cfg)
    if not ok:
        return None
    devices = poll_devices(session, base)
    oui_enabled = cfg.get("monitor", {}).get("oui_lookup_enabled", True)
    for mac, info in devices.items():
        info["manufacturer"] = lookup_manufacturer(mac, enabled=oui_enabled)
    return devices


# ---------------------------------------------------------------------------
# MAC OUI (manufacturer) lookup
# ---------------------------------------------------------------------------
_OUI_CACHE = {}


def load_oui_cache():
    global _OUI_CACHE
    _OUI_CACHE = load_json(OUI_CACHE_FILE, {})


def lookup_manufacturer(mac, enabled=True):
    """Look up the device manufacturer from the MAC OUI (first 3 octets)."""
    if not enabled:
        return "Unknown"

    mac = mac.lower().strip()
    oui = ":".join(mac.split(":")[:3])

    if oui in _OUI_CACHE:
        return _OUI_CACHE[oui]

    manufacturer = "Unknown"
    try:
        resp = requests.get(
            f"https://api.macvendors.com/{mac}",
            timeout=5,
            headers={"User-Agent": "RouterMonitor/1.0"},
        )
        if resp.status_code == 200 and resp.text.strip():
            manufacturer = resp.text.strip()
            if len(manufacturer) > 60:
                manufacturer = manufacturer[:57] + "..."
    except Exception:
        pass

    _OUI_CACHE[oui] = manufacturer
    save_json(OUI_CACHE_FILE, _OUI_CACHE)
    return manufacturer


# ---------------------------------------------------------------------------
# Device naming
# ---------------------------------------------------------------------------
def get_device_name(mac, cfg, devices_info=None):
    """
    Get a friendly device name. Priority:
      1. Custom name set by user (via /name command or config file)
      2. Hostname reported by the router (from EasyMeshAPDetails)
      3. Manufacturer from OUI lookup
      4. MAC address as last resort
    """
    if devices_info is None:
        devices_info = {}
    custom_names = cfg.get("device_names", {})
    mac_lower = mac.lower()

    # 1. Custom user-set name
    if mac_lower in custom_names:
        return custom_names[mac_lower]

    # 2. Hostname from router
    hostname = devices_info.get(mac, {}).get("hostname", "")
    if hostname:
        return hostname

    # 3. Manufacturer from OUI
    manufacturer = devices_info.get(mac, {}).get("manufacturer", "Unknown")
    if manufacturer and manufacturer != "Unknown":
        return manufacturer

    # 4. Fallback
    return f"Unknown ({mac})"


def signal_label(rssi):
    if not isinstance(rssi, int):
        return "?"
    if rssi >= -50:
        return "Excellent"
    elif rssi >= -60:
        return "Good"
    elif rssi >= -70:
        return "Fair"
    else:
        return "Weak"


# ---------------------------------------------------------------------------
# Telegram — sending messages
# ---------------------------------------------------------------------------
def send_telegram(cfg, text, parse_mode="HTML"):
    """Send a message via the Telegram bot API."""
    tg = cfg["telegram"]
    token = tg["bot_token"]
    chat_id = tg["chat_id"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        if resp.status_code != 200:
            log(f"  WARN: Telegram API returned {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        log(f"  ERROR: could not send Telegram message: {e}")
        return False


# ---------------------------------------------------------------------------
# Telegram — receiving commands (long-polling)
# ---------------------------------------------------------------------------
_last_update_id = 0


def init_update_offset(cfg):
    """On startup, skip any old messages by setting offset past the last update."""
    global _last_update_id
    tg = cfg["telegram"]
    token = tg["bot_token"]
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"timeout": 0, "limit": 1},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok") and data.get("result"):
            _last_update_id = max(u["update_id"] for u in data["result"])
            # Acknowledge (consume) old updates so they aren't reprocessed
            requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": _last_update_id + 1, "timeout": 0, "limit": 1},
                timeout=5,
            )
            log(f"  Telegram: skipped {len(data['result'])} old update(s), starting at offset {_last_update_id + 1}")
    except Exception as e:
        log(f"  WARN: could not init Telegram offset: {e}")


def poll_telegram_updates(cfg, timeout=2):
    """
    Long-poll Telegram getUpdates for up to *timeout* seconds.
    Returns a list of (chat_id, text) tuples for any messages received.
    """
    global _last_update_id
    tg = cfg["telegram"]
    token = tg["bot_token"]
    messages = []

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={
                "offset": _last_update_id + 1,
                "timeout": timeout,
                "limit": 10,
            },
            timeout=timeout + 5,  # buffer beyond the long-poll timeout
        )
        data = resp.json()
        if not data.get("ok"):
            return messages

        for update in data.get("result", []):
            _last_update_id = update["update_id"]
            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "").strip()
            if chat_id and text:
                messages.append((chat_id, text))
    except requests.exceptions.Timeout:
        pass  # normal for long-polling — no messages
    except Exception as e:
        log(f"  WARN: Telegram getUpdates error: {e}")

    return messages


# ---------------------------------------------------------------------------
# Router management functions (WiFi, blocking, ports, WAN)
# ---------------------------------------------------------------------------
def _router_session(cfg):
    """Create an authenticated router session and return (session, base)."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    ok, base = router_login(session, cfg)
    if not ok:
        return None, None
    return session, base


_HIDDEN_RE = re.compile(
    r'<INPUT[^>]*NAME=["\']?(\w+)["\']?[^>]*VALUE=["\']([^"\']*)["\']',
    re.IGNORECASE,
)


def _extract_hidden_fields(html):
    """Extract all hidden INPUT field name→value pairs from HTML."""
    return {name: val for name, val in _HIDDEN_RE.findall(html)}


def _parse_table(html):
    """Parse HTML <table> rows into list of cell-lists."""
    rows = []
    for tr in re.finditer(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', tr.group(1), re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        if cells and any(c for c in cells):
            rows.append(cells)
    return rows


# ── WiFi settings ────────────────────────────────────────────────────────────

def get_wifi_settings(cfg, band="2.4ghz"):
    """Fetch current WiFi SSID, password, security for a band."""
    session, base = _router_session(cfg)
    if not session:
        return None
    prefix = "wlan" if band == "2.4ghz" else "wlan5"
    resp = session.get(f"{base}/cgi-bin/{prefix}_general.cgi?SSID_INDEX=0",
                       headers={"Referer": f"{base}/cgi-bin/tabFW.cgi"},
                       timeout=10, verify=False)
    if "login_advance" in resp.text:
        return None
    fields = _extract_hidden_fields(resp.text)

    # Values may be injected via decodeURIComponent("...") in JS (esp. 5GHz)
    decoded = [urllib.parse.unquote(v) for v in
               re.findall(r'decodeURIComponent\(["\']([^"\']+)["\']\)', resp.text)]

    ssid = (fields.get("SSID") or fields.get("OldESSID")
            or fields.get("Old_SSID") or "")
    password = fields.get("PreSharedKey") or fields.get("OldWLANCfgPSK") or ""

    # Fall back to decoded JS values if the form fields were empty
    if not ssid or len(ssid) < 2:
        for d in decoded:
            if 2 <= len(d) <= 32 and "@" not in d and "%" not in d:
                ssid = d
                break
    if not password or len(password) < 8:
        for d in decoded:
            if len(d) >= 8 and ("@" in d or "#" in d or d.isalnum()):
                password = d
                break

    security = fields.get("SecMode") or fields.get("OldAuthMode", "?")
    encryption = fields.get("EncrypTypeMode") or fields.get("OldEncrypTypeMode", "?")
    channel = fields.get("OldChannel", "?")
    return {
        "ssid": ssid or "?", "password": password, "security": security,
        "encryption": encryption, "channel": channel, "band": band,
        "all_fields": fields,
    }


def change_wifi_ssid(cfg, new_ssid, band="2.4ghz"):
    """Change the WiFi SSID name."""
    current = get_wifi_settings(cfg, band)
    if not current:
        return False, "Could not read current WiFi settings"
    fields = current["all_fields"]
    fields["SSID"] = new_ssid
    fields["saveflag"] = "1"
    session, base = _router_session(cfg)
    if not session:
        return False, "Could not log in"
    prefix = "wlan" if band == "2.4ghz" else "wlan5"
    resp = session.post(f"{base}/cgi-bin/{prefix}_general.cgi",
                        data=fields,
                        headers={"Referer": f"{base}/cgi-bin/{prefix}_general.cgi?SSID_INDEX=0"},
                        timeout=10, verify=False)
    return True, f"WiFi {'5GHz' if band == '5ghz' else '2.4GHz'} SSID changed to '{new_ssid}'"


def change_wifi_password(cfg, new_password, band="2.4ghz"):
    """Change the WiFi password."""
    current = get_wifi_settings(cfg, band)
    if not current:
        return False, "Could not read current WiFi settings"
    fields = current["all_fields"]
    fields["PreSharedKey"] = new_password
    fields["saveflag"] = "1"
    session, base = _router_session(cfg)
    if not session:
        return False, "Could not log in"
    prefix = "wlan" if band == "2.4ghz" else "wlan5"
    resp = session.post(f"{base}/cgi-bin/{prefix}_general.cgi",
                        data=fields,
                        headers={"Referer": f"{base}/cgi-bin/{prefix}_general.cgi?SSID_INDEX=0"},
                        timeout=10, verify=False)
    return True, f"WiFi {'5GHz' if band == '5ghz' else '2.4GHz'} password changed successfully"


# ── Device blocking (Parental Control) ───────────────────────────────────────

def get_blocked_devices(cfg):
    """List parental control / blocked device rules."""
    session, base = _router_session(cfg)
    if not session:
        return None
    resp = session.get(f"{base}/cgi-bin/PCP_list.cgi",
                       headers={"Referer": f"{base}/cgi-bin/ParentalControl.cgi"},
                       timeout=10, verify=False)
    if "login_advance" in resp.text:
        return None
    return _parse_table(resp.text)


def block_device(cfg, mac_address, name="Blocked"):
    """Block a device from internet access via parental control."""
    session, base = _router_session(cfg)
    if not session:
        return False, "Could not log in"
    mac = mac_address.lower().strip()
    data = {
        "applyflag": "1",
        "PCPenableck": "Enable",
        "enableck": "on",
        "pcpname": name,
        "userselect": mac,
        "FWRCfg_Everyday": "on",
        "FWRCfg_Everyday_H": "Yes",
        "FWRCfg_Sun": "on", "FWRCfg_Sun_H": "Yes",
        "FWRCfg_Mon": "on", "FWRCfg_Mon_H": "Yes",
        "FWRCfg_Tue": "on", "FWRCfg_Tue_H": "Yes",
        "FWRCfg_Wed": "on", "FWRCfg_Wed_H": "Yes",
        "FWRCfg_Thu": "on", "FWRCfg_Thu_H": "Yes",
        "FWRCfg_Fri": "on", "FWRCfg_Fri_H": "Yes",
        "FWRCfg_Sat": "on", "FWRCfg_Sat_H": "Yes",
        "PtCtrl_Time_Start_hour": "0", "PtCtrl_Time_Start_minute": "0",
        "PtCtrl_Time_End_hour": "23", "PtCtrl_Time_End_minute": "59",
        "serviceAction": "1",
        "PCP_Active1": "Yes", "pcpActive1": "on",
        "addbtn": "1",
    }
    resp = session.post(f"{base}/cgi-bin/ParentalControl.cgi",
                        data=data,
                        headers={"Referer": f"{base}/cgi-bin/ParentalControladd.cgi"},
                        timeout=10, verify=False)
    return True, f"Device {mac} has been blocked from internet access"


def unblock_device(cfg, rule_num):
    """Unblock a device by deleting the parental control rule."""
    session, base = _router_session(cfg)
    if not session:
        return False, "Could not log in"
    data = {"delFlag": "1", "delnum": str(rule_num)}
    resp = session.post(f"{base}/cgi-bin/delete.cgi",
                        data=data,
                        headers={"Referer": f"{base}/cgi-bin/ParentalControl.cgi"},
                        timeout=10, verify=False)
    return True, f"Rule #{rule_num} deleted — device unblocked"


# ── Port forwarding ──────────────────────────────────────────────────────────

def get_port_forwarding(cfg):
    """List all port forwarding rules."""
    session, base = _router_session(cfg)
    if not session:
        return None
    resp = session.get(f"{base}/cgi-bin/port_forwarding_list.cgi",
                       headers={"Referer": f"{base}/cgi-bin/portForwarding.cgi"},
                       timeout=10, verify=False)
    if "login_advance" in resp.text:
        return None
    return _parse_table(resp.text)


# ── WAN status ───────────────────────────────────────────────────────────────

def get_wan_status(cfg):
    """Get WAN connection status (IP, type, NAT from broadband.cgi)."""
    session, base = _router_session(cfg)
    if not session:
        return None
    resp = session.get(f"{base}/cgi-bin/broadband.cgi",
                       headers={"Referer": f"{base}/cgi-bin/tabFW.cgi"},
                       timeout=10, verify=False)
    if "login_advance" in resp.text:
        return None
    rows = _parse_table(resp.text)
    # Find the row that looks like a WAN connection (has IP address)
    wan_rows = []
    for row in rows:
        for cell in row:
            if re.match(r'\d+\.\d+\.\d+\.\d+', cell) or re.match(r'[0-9a-f:]{20,}', cell):
                wan_rows.append(row)
                break
    # Also extract IP from JS decodeURIComponent
    decoded = [urllib.parse.unquote(v) for v in
               re.findall(r'decodeURIComponent\(["\']([^"\']+)["\']\)', resp.text)]
    ipv6 = [d for d in decoded if ":" in d and len(d) > 20]
    return {"rows": wan_rows, "all_rows": rows, "ipv6": ipv6}


# ── Router reboot ────────────────────────────────────────────────────────────

def reboot_router(cfg):
    """Reboot the router via POST /cgi-bin/reboot.cgi."""
    session, base = _router_session(cfg)
    if not session:
        return False, "Could not log in"
    data = {"restoreFlag": "1", "RestartBtn": "RESTART"}
    resp = session.post(f"{base}/cgi-bin/reboot.cgi",
                        data=data,
                        headers={"Referer": f"{base}/cgi-bin/reboot.cgi"},
                        timeout=10, verify=False)
    return True, "Router is rebooting. WiFi will be down for ~2 minutes."


# ── Firewall ─────────────────────────────────────────────────────────────────

def get_firewall_status(cfg):
    """Get firewall status from TELFirewall_general.cgi."""
    session, base = _router_session(cfg)
    if not session:
        return None
    resp = session.get(f"{base}/cgi-bin/TELFirewall_general.cgi",
                       headers={"Referer": f"{base}/cgi-bin/tabFW.cgi"},
                       timeout=10, verify=False)
    if "login_advance" in resp.text:
        return None
    fields = _extract_hidden_fields(resp.text)
    enabled = fields.get("FirewallFlag", "0") == "1"
    return {"enabled": enabled, "raw_fields": fields}


def set_firewall(cfg, enable=True):
    """Enable or disable the firewall."""
    session, base = _router_session(cfg)
    if not session:
        return False, "Could not log in"
    data = {"FirewallFlag": "1" if enable else "0", "FirewallRadioFlag": "Yes" if enable else "No"}
    resp = session.post(f"{base}/cgi-bin/TELFirewall_general.cgi",
                        data=data,
                        headers={"Referer": f"{base}/cgi-bin/TELFirewall_general.cgi"},
                        timeout=10, verify=False)
    return True, f"Firewall {'enabled' if enable else 'disabled'}"


# ── System info ──────────────────────────────────────────────────────────────

def get_system_info(cfg):
    """Get system info (model, date/time from time.cgi)."""
    session, base = _router_session(cfg)
    if not session:
        return None
    resp = session.get(f"{base}/cgi-bin/time.cgi",
                       headers={"Referer": f"{base}/cgi-bin/tabFW.cgi"},
                       timeout=10, verify=False)
    if "login_advance" in resp.text:
        return None
    fields = _extract_hidden_fields(resp.text)
    return {"fields": fields, "raw": resp.text}


# ---------------------------------------------------------------------------
# Telegram — command handlers
# ---------------------------------------------------------------------------
def handle_command(cfg, chat_id, text):
    """Parse and handle a single Telegram command."""
    tg = cfg["telegram"]
    # Only respond to the configured chat ID (security)
    if str(chat_id) != str(tg["chat_id"]):
        log(f"  Ignoring message from unauthorized chat_id={chat_id}")
        return

    parts = text.split(None, 3)  # split into at most 4 parts
    cmd = parts[0].lower().split("@")[0]  # strip bot mention suffix

    log(f"  Telegram command: {text}")

    if cmd in ("/devices", "/list", "/connected"):
        cmd_devices(cfg)
    elif cmd in ("/status", "/info"):
        cmd_status(cfg)
    elif cmd in ("/wifi", "/wifi_settings"):
        cmd_wifi(cfg, "2.4ghz")
    elif cmd in ("/wifi5g", "/wifi5"):
        cmd_wifi(cfg, "5ghz")
    elif cmd in ("/wifi_name", "/ssid"):
        cmd_wifi_name(cfg, parts)
    elif cmd in ("/wifi_password", "/wifipass", "/wifi_pass"):
        cmd_wifi_password(cfg, parts)
    elif cmd == "/block":
        cmd_block(cfg, parts)
    elif cmd == "/unblock":
        cmd_unblock(cfg, parts)
    elif cmd in ("/blocked", "/blocks"):
        cmd_blocked(cfg)
    elif cmd in ("/ports", "/portforward"):
        cmd_ports(cfg)
    elif cmd in ("/wan", "/internet"):
        cmd_wan(cfg)
    elif cmd in ("/firewall", "/fw"):
        cmd_firewall(cfg)
    elif cmd in ("/firewall_on", "/fw_on"):
        cmd_firewall_toggle(cfg, True)
    elif cmd in ("/firewall_off", "/fw_off"):
        cmd_firewall_toggle(cfg, False)
    elif cmd == "/reboot":
        cmd_reboot(cfg)
    elif cmd == "/confirm_reboot":
        cmd_confirm_reboot(cfg)
    elif cmd == "/name":
        cmd_set_name(cfg, parts)
    elif cmd in ("/names", "/labels"):
        cmd_list_names(cfg)
    elif cmd == "/refresh":
        cmd_refresh(cfg)
    elif cmd == "/help":
        cmd_help(cfg)
    elif cmd == "/start":
        send_telegram(cfg, (
            "👋 <b>Router Monitor Bot</b>\n\n"
            "I monitor your Airtel Fiber router for connected devices.\n\n"
            "Send /help to see available commands."
        ))
    else:
        # Ignore non-command messages silently
        pass


def cmd_devices(cfg):
    """Handle /devices — list currently connected devices."""
    devices = fetch_current_devices(cfg)
    if devices is None:
        send_telegram(cfg, "❌ Could not log in to the router. Please check connectivity.")
        return

    if not devices:
        send_telegram(cfg, "📭 <b>No devices currently connected</b> to your router.")
        return

    lines = [f"📱 <b>Connected Devices ({len(devices)})</b>\n"]
    for i, (mac, info) in enumerate(devices.items(), 1):
        name = get_device_name(mac, cfg, devices)
        rssi = info.get("rssi", "?")
        sig = signal_label(rssi)
        ip = info.get("ip", "")
        ssid = info.get("ssid", "?")
        ip_line = f"\n   🌐 IP: <code>{ip}</code>" if ip else ""
        lines.append(
            f"{i}. <b>{name}</b>\n"
            f"   🏷️ <code>{mac}</code>\n"
            f"   📶 {ssid} • 📊 {rssi} dBm ({sig})"
            f"{ip_line}"
        )
    send_telegram(cfg, "\n\n".join(lines))


def cmd_status(cfg):
    """Handle /status — device list + monitor status."""
    state = load_json(STATE_FILE, {})
    last_updated = state.get("last_updated", "never")
    devices = fetch_current_devices(cfg)

    if devices is None:
        send_telegram(cfg, "❌ Could not log in to the router. Please check connectivity.")
        return

    count = len(devices)
    lines = [f"📊 <b>Router Monitor Status</b>\n"]
    lines.append(f"🔄 Last auto-poll: {last_updated}")
    lines.append(f"📡 Devices connected now: {count}\n")

    if devices:
        lines.append("<b>Connected devices:</b>")
        for i, (mac, info) in enumerate(devices.items(), 1):
            name = get_device_name(mac, cfg, devices)
            rssi = info.get("rssi", "?")
            lines.append(f"  {i}. {name} — <code>{mac}</code> ({info.get('band','?')}, {rssi} dBm)")
    else:
        lines.append("No devices connected.")

    send_telegram(cfg, "\n".join(lines))


def cmd_set_name(cfg, parts):
    """Handle /name <mac> <name> — assign a custom device name."""
    # Expected: /name <mac> <name...>
    if len(parts) < 3:
        send_telegram(cfg, (
            "Usage: <code>/name &lt;MAC&gt; &lt;name&gt;</code>\n\n"
            "Example: <code>/name aa:bb:cc:dd:ee:ff My Phone</code>"
        ))
        return

    mac = parts[1].lower().strip()
    name = " ".join(parts[2:]).strip()

    # Validate MAC format
    if not re.match(r'^[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$', mac):
        send_telegram(cfg, f"❌ Invalid MAC format.\nUse format: <code>aa:bb:cc:dd:ee:ff</code>\nYou sent: <code>{mac}</code>")
        return

    # Update config
    if "device_names" not in cfg:
        cfg["device_names"] = {}
    old_name = cfg["device_names"].get(mac)
    cfg["device_names"][mac] = name
    save_config(cfg)

    if old_name:
        send_telegram(cfg, f"✅ Renamed device\n<code>{mac}</code>\nOld: {old_name}\nNew: <b>{name}</b>")
    else:
        send_telegram(cfg, f"✅ Named device\n<code>{mac}</code> → <b>{name}</b>")
    log(f"  Set name: {mac} → {name}")


def cmd_list_names(cfg):
    """Handle /names — list all custom device names."""
    names = cfg.get("device_names", {})
    if not names:
        send_telegram(cfg, "📝 No custom device names set.\nUse <code>/name &lt;MAC&gt; &lt;name&gt;</code> to add one.")
        return

    lines = ["📝 <b>Custom Device Names</b>\n"]
    for i, (mac, name) in enumerate(names.items(), 1):
        lines.append(f"{i}. <b>{name}</b>\n   <code>{mac}</code>")
    send_telegram(cfg, "\n".join(lines))


def cmd_refresh(cfg):
    """Handle /refresh — force an immediate device poll and report changes."""
    send_telegram(cfg, "🔄 Polling router for devices now...")
    devices = run_monitor_cycle(cfg, force=True)
    if devices is None:
        send_telegram(cfg, "❌ Could not log in to the router.")
        return
    count = len(devices)
    if count:
        names = [get_device_name(mac, cfg, devices) for mac in devices]
        send_telegram(cfg, f"✅ Poll complete. {count} device(s) connected:\n" + "\n".join(f"  • {n}" for n in names))
    else:
        send_telegram(cfg, "✅ Poll complete. No devices connected.")


def cmd_help(cfg):
    """Handle /help — show available commands."""
    send_telegram(cfg, (
        "🤖 <b>Router Control Bot — All Commands</b>\n\n"
        "📡 <b>Devices:</b>\n"
        "/devices — List connected devices\n"
        "/status — Full router status\n"
        "/refresh — Force device poll now\n\n"
        "📶 <b>WiFi Settings:</b>\n"
        "/wifi — View 2.4GHz WiFi (SSID, password, security)\n"
        "/wifi5g — View 5GHz WiFi settings\n"
        "/wifi_name &lt;new_name&gt; — Change WiFi SSID\n"
        "/wifi_password &lt;new_pass&gt; — Change WiFi password\n\n"
        "🚫 <b>Device Blocking:</b>\n"
        "/block &lt;MAC&gt; — Block device from internet\n"
        "/unblock &lt;rule#&gt; — Unblock device\n"
        "/blocked — List blocked devices\n\n"
        "🛡️ <b>Firewall:</b>\n"
        "/firewall — View firewall status\n"
        "/firewall_on — Enable firewall\n"
        "/firewall_off — Disable firewall\n\n"
        "🔌 <b>Network:</b>\n"
        "/ports — List port forwarding rules\n"
        "/wan — View internet/WAN status (IP, connection type)\n\n"
        "🔄 <b>System:</b>\n"
        "/reboot — Reboot router (with confirmation)\n\n"
        "🏷️ <b>Device Names:</b>\n"
        "/name &lt;MAC&gt; &lt;name&gt; — Name a device\n"
        "/names — Show all custom names\n\n"
        "/help — Show this help\n\n"
        "💡 I also send automatic alerts when new devices connect!"
    ))


# ---------------------------------------------------------------------------
# New command handlers — WiFi, blocking, ports, WAN
# ---------------------------------------------------------------------------
def cmd_wifi(cfg, band):
    """Handle /wifi or /wifi5g — show WiFi settings."""
    settings = get_wifi_settings(cfg, band)
    if not settings:
        send_telegram(cfg, "❌ Could not fetch WiFi settings from router.")
        return
    band_label = "5GHz" if band == "5ghz" else "2.4GHz"
    text = (
        f"📶 <b>WiFi {band_label} Settings</b>\n\n"
        f"📡 <b>SSID:</b> {settings['ssid']}\n"
        f"🔑 <b>Password:</b> <code>{settings['password']}</code>\n"
        f"🔒 <b>Security:</b> {settings['security']}\n"
        f"🔐 <b>Encryption:</b> {settings['encryption']}\n"
        f"📊 <b>Channel:</b> {settings['channel']} (0 = auto)"
    )
    send_telegram(cfg, text)


def cmd_wifi_name(cfg, parts):
    """Handle /wifi_name <new_name> — change WiFi SSID."""
    if len(parts) < 2:
        send_telegram(cfg, "Usage: <code>/wifi_name &lt;new_name&gt;</code>\nExample: <code>/wifi_name MyHomeWiFi</code>")
        return
    new_name = " ".join(parts[1:]).strip()
    ok, msg = change_wifi_ssid(cfg, new_name, "2.4ghz")
    if ok:
        send_telegram(cfg, f"✅ {msg}\n\n⚠️ Devices will need to reconnect with the new WiFi name.")
    else:
        send_telegram(cfg, f"❌ {msg}")


def cmd_wifi_password(cfg, parts):
    """Handle /wifi_password <new_password> — change WiFi password."""
    if len(parts) < 2:
        send_telegram(cfg, "Usage: <code>/wifi_password &lt;new_password&gt;</code>\nExample: <code>/wifi_password MyNewPass123</code>")
        return
    new_pass = " ".join(parts[1:]).strip()
    if len(new_pass) < 8:
        send_telegram(cfg, "❌ Password must be at least 8 characters.")
        return
    ok, msg = change_wifi_password(cfg, new_pass, "2.4ghz")
    if ok:
        send_telegram(cfg, f"✅ {msg}\n\n⚠️ All devices will need to reconnect with the new password.")
    else:
        send_telegram(cfg, f"❌ {msg}")


def cmd_block(cfg, parts):
    """Handle /block <MAC> — block a device from internet."""
    if len(parts) < 2:
        send_telegram(cfg, "Usage: <code>/block &lt;MAC&gt;</code>\nExample: <code>/block aa:bb:cc:dd:ee:ff</code>")
        return
    mac = parts[1].lower().strip()
    if not re.match(r'^[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$', mac):
        send_telegram(cfg, f"❌ Invalid MAC format. Use: <code>aa:bb:cc:dd:ee:ff</code>")
        return
    name = " ".join(parts[2:]).strip() if len(parts) > 2 else "Blocked"
    send_telegram(cfg, f"🚫 Blocking device {mac}...")
    ok, msg = block_device(cfg, mac, name)
    if ok:
        send_telegram(cfg, f"✅ {msg}")
    else:
        send_telegram(cfg, f"❌ {msg}")


def cmd_unblock(cfg, parts):
    """Handle /unblock <rule#> — unblock a device."""
    if len(parts) < 2:
        send_telegram(cfg, "Usage: <code>/unblock &lt;rule_number&gt;</code>\nUse /blocked to see rule numbers.")
        return
    try:
        rule_num = int(parts[1])
    except ValueError:
        send_telegram(cfg, "❌ Rule number must be a number. Use /blocked to see rule numbers.")
        return
    ok, msg = unblock_device(cfg, rule_num)
    if ok:
        send_telegram(cfg, f"✅ {msg}")
    else:
        send_telegram(cfg, f"❌ {msg}")


def cmd_blocked(cfg):
    """Handle /blocked — list blocked devices."""
    rules = get_blocked_devices(cfg)
    if rules is None:
        send_telegram(cfg, "❌ Could not fetch parental control rules.")
        return
    if len(rules) <= 1:  # only header row
        send_telegram(cfg, "📭 <b>No devices blocked.</b>\nUse <code>/block &lt;MAC&gt;</code> to block a device.")
        return
    lines = ["🚫 <b>Blocked Devices</b>\n"]
    for row in rules[1:]:  # skip header
        lines.append(f"  {' | '.join(row)}")
    send_telegram(cfg, "\n".join(lines))


def cmd_ports(cfg):
    """Handle /ports — list port forwarding rules."""
    rules = get_port_forwarding(cfg)
    if rules is None:
        send_telegram(cfg, "❌ Could not fetch port forwarding rules.")
        return
    if len(rules) <= 1:
        send_telegram(cfg, "📭 <b>No port forwarding rules.</b>")
        return
    lines = ["🔌 <b>Port Forwarding Rules</b>\n"]
    for row in rules[1:]:
        lines.append(f"  {' | '.join(row)}")
    send_telegram(cfg, "\n".join(lines))


def cmd_wan(cfg):
    """Handle /wan — view WAN/internet status."""
    wan = get_wan_status(cfg)
    if not wan:
        send_telegram(cfg, "❌ Could not fetch WAN status.")
        return
    lines = ["🌐 <b>WAN / Internet Status</b>\n"]
    for row in wan.get("rows", []):
        lines.append(f"  {' | '.join(row)}")
    if wan.get("ipv6"):
        lines.append(f"\nIPv6: {wan['ipv6'][0]}")
    if not wan.get("rows") and not wan.get("ipv6"):
        lines.append("No WAN connection data found.")
    send_telegram(cfg, "\n".join(lines))


def cmd_firewall(cfg):
    """Handle /firewall — show firewall status."""
    fw = get_firewall_status(cfg)
    if fw is None:
        send_telegram(cfg, "❌ Could not fetch firewall status.")
        return
    status = "🟢 ENABLED" if fw["enabled"] else "🔴 DISABLED"
    send_telegram(cfg, (
        f"🛡️ <b>Firewall Status</b>\n\n"
        f"State: {status}\n\n"
        f"Use /firewall_on to enable\n"
        f"Use /firewall_off to disable"
    ))


def cmd_firewall_toggle(cfg, enable):
    """Handle /firewall_on or /firewall_off."""
    ok, msg = set_firewall(cfg, enable)
    if ok:
        send_telegram(cfg, f"✅ {msg}")
    else:
        send_telegram(cfg, f"❌ {msg}")


def cmd_reboot(cfg):
    """Handle /reboot — reboot the router with confirmation."""
    send_telegram(cfg, (
        "⚠️ <b>Reboot Router?</b>\n\n"
        "This will disconnect ALL devices for ~2 minutes.\n\n"
        "Send /confirm_reboot to proceed, or ignore to cancel."
    ))


def cmd_confirm_reboot(cfg):
    """Handle /confirm_reboot — actually reboot the router."""
    send_telegram(cfg, "🔄 Rebooting router now...")
    ok, msg = reboot_router(cfg)
    if ok:
        send_telegram(cfg, f"✅ {msg}\n\nThe bot will reconnect automatically once the router is back up.")
    else:
        send_telegram(cfg, f"❌ {msg}")


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------
def notify_new_device(mac, info, cfg):
    """Send a Telegram notification for a newly connected device."""
    name = get_device_name(mac, cfg, {mac: info})
    rssi = info.get("rssi", "?")
    ssid = info.get("ssid", "?")
    ip = info.get("ip", "")
    sig = signal_label(rssi)

    ip_line = f"\n🌐 <b>IP:</b> <code>{ip}</code>" if ip else ""
    text = (
        f"🔔 <b>New device connected to your router!</b>\n\n"
        f"📱 <b>Device:</b> {name}\n"
        f"🏷️ <b>MAC:</b> <code>{mac}</code>\n"
        f"📡 <b>SSID:</b> {ssid}\n"
        f"📊 <b>Signal:</b> {rssi} dBm ({sig})"
        f"{ip_line}\n"
        f"🕐 <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    send_telegram(cfg, text)
    log(f"  → Notified Telegram: new device {mac} ({name})")


def notify_device_left(mac, info, cfg):
    """Send a Telegram notification for a disconnected device."""
    name = get_device_name(mac, cfg, {mac: info})
    text = (
        f"📤 <b>Device disconnected from your router</b>\n\n"
        f"📱 <b>Device:</b> {name}\n"
        f"🏷️ <b>MAC:</b> <code>{mac}</code>\n"
        f"🕐 <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    send_telegram(cfg, text)
    log(f"  → Notified Telegram: device left {mac} ({name})")


# ---------------------------------------------------------------------------
# Monitoring cycle
# ---------------------------------------------------------------------------
def run_monitor_cycle(cfg, force=False):
    """
    Run one device-polling cycle: login, poll, detect changes, notify, save state.
    Returns the devices dict (or None on login failure).

    If *force* is True, skip the first-run suppression so /refresh always reports.
    """
    devices = fetch_current_devices(cfg)
    if devices is None:
        log("  Login failed — skipping this cycle")
        if not force:
            send_telegram(
                cfg,
                "⚠️ <b>Router Monitor: Login Failed</b>\n\n"
                "Could not log in to the router. Check credentials and connectivity.",
            )
        return None

    log(f"  Found {len(devices)} connected device(s)"
        + (f": {', '.join(devices.keys())}" if devices else ""))

    # Load previous state
    prev_state = load_json(STATE_FILE, {})
    prev_devices = prev_state.get("devices", {})
    first_run = prev_state.get("first_run", True) and not force

    # Detect changes
    current_macs = set(devices.keys())
    prev_macs = set(prev_devices.keys())
    new_macs = current_macs - prev_macs
    gone_macs = prev_macs - current_macs

    notify_connect = cfg.get("monitor", {}).get("notify_on_connect", True)
    notify_disconnect = cfg.get("monitor", {}).get("notify_on_disconnect", True)

    if first_run:
        log("  First run — recording current devices without notifications")
        if cfg.get("monitor", {}).get("send_startup_message", True):
            if devices:
                lines = [f"🚀 <b>Router Monitor started</b>\n"]
                lines.append(f"📡 Currently {len(devices)} device(s) connected:\n")
                for mac, info in devices.items():
                    name = get_device_name(mac, cfg, devices)
                    lines.append(f"  • {name} (<code>{mac}</code>) — {info['band']}")
                lines.append(f"\nI will notify you when new devices connect. ✅")
                lines.append(f"Send /help for commands.")
                send_telegram(cfg, "\n".join(lines))
            else:
                send_telegram(cfg, "🚀 <b>Router Monitor started</b>\n\nNo devices currently connected. I will notify you when a device connects. ✅\nSend /help for commands.")
    else:
        if notify_connect:
            for mac in new_macs:
                notify_new_device(mac, devices[mac], cfg)
        if notify_disconnect:
            for mac in gone_macs:
                old_info = prev_devices.get(mac, {})
                old_info.setdefault("manufacturer", lookup_manufacturer(mac, enabled=cfg.get("monitor", {}).get("oui_lookup_enabled", True)))
                notify_device_left(mac, old_info, cfg)

    # Save state
    save_json(STATE_FILE, {
        "devices": devices,
        "last_updated": datetime.now().isoformat(),
        "first_run": False,
    })

    return devices


# ---------------------------------------------------------------------------
# Main loop (continuous mode with bot)
# ---------------------------------------------------------------------------
def main():
    log("=" * 60)
    log("Airtel Fiber Router Device Monitor + Telegram Bot")
    log("=" * 60)

    cfg = load_config()
    load_oui_cache()

    # --- Single-run mode (for testing / LaunchAgent --once) ---
    if "--once" in sys.argv:
        run_monitor_cycle(cfg)
        return

    # --- Continuous mode: bot + monitor ---
    interval = cfg.get("monitor", {}).get("poll_interval_seconds", 60)
    log(f"  Monitor interval: {interval}s | Telegram bot: active")

    # Initialize Telegram offset (skip old messages)
    init_update_offset(cfg)

    # Send startup message
    if cfg.get("monitor", {}).get("send_startup_message", True):
        send_telegram(cfg, "🤖 <b>Router Monitor Bot is online!</b>\nSend /help for commands. I'll notify you when devices connect.")

    last_poll_time = 0
    log("  Entering main loop...")

    while True:
        try:
            # 1. Poll Telegram for commands (long-poll, blocks up to 2s)
            messages = poll_telegram_updates(cfg, timeout=2)
            for chat_id, text in messages:
                handle_command(cfg, chat_id, text)

            # 2. Check if it's time for a router poll
            now = time.time()
            if now - last_poll_time >= interval:
                run_monitor_cycle(cfg)
                last_poll_time = now

        except KeyboardInterrupt:
            log("  Stopped by user.")
            break
        except Exception as e:
            log(f"  ERROR in main loop: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(5)  # back off on errors


if __name__ == "__main__":
    main()
