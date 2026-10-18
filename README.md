# Airtel Fiber Router Monitor & Management Bot

Monitor and manage your Airtel Fiber router from anywhere using a Telegram bot. Get instant alerts when new devices connect to your WiFi, change your WiFi name and password without opening a browser, block devices from the internet, toggle your firewall, check your public IP, and reboot your router — all through simple chat commands on Telegram.

Designed for Airtel Xstream Fiber routers with Zyxel-based ONT firmware. The tool reverse-engineers the router's MD5 challenge-response authentication and communicates directly with internal CGI endpoints. No browser automation, no Selenium, no official API required. It runs as a background service on any machine and polls the router every 60 seconds for connected devices.

```
You (Telegram)                        Your machine                       Router (192.168.1.1)
     |                                     |                                    |
     |-- /devices -----------------------> |                                    |
     |                                     |-- GET EasyMeshAPDetails.cgi ------> |
     |<-- "2 devices: iPhone, Mac" ------ |                                    |
     |                                     |                                    |
     |-- /wifi_name MyHome --------------> |                                    |
     |                                     |-- POST wlan_general.cgi ----------> |
     |<-- "WiFi SSID changed" ----------- |                                    |
     |                                     |                                    |
     |-- /reboot ------------------------> |                                    |
     |                                     |-- POST reboot.cgi ----------------> |
     |<-- "Router is rebooting" --------- |                                    |
```

## Quick Start

```bash
git clone https://github.com/S33NUU/airtel-router-monitor.git
cd airtel-router-monitor
pip install -r requirements.txt
cp router_monitor_config.example.json router_monitor_config.json
```

Edit `router_monitor_config.json` with your router password and Telegram bot token, then:

```bash
python3 router_monitor.py
```

Send `/help` to your bot on Telegram. That's it.

### Get a Telegram bot token

1. Open Telegram, search **@BotFather**, send `/newbot`
2. Copy the token it gives you
3. Send any message to your new bot
4. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` — your `chat_id` is in the response

## Example Output

```
You: /devices
Bot: Connected Devices (2)

  1. iPhone
     IP: 192.168.1.x | MAC: aa:bb:cc:dd:ee:ff
     Signal: -46 dBm (Excellent) | SSID: Home WiFi

  2. Mac
     IP: 192.168.1.x | MAC: 11:22:33:44:55:66
     Signal: -56 dBm (Good) | SSID: Home WiFi

You: /wifi
Bot: WiFi 2.4GHz Settings

  SSID: Home WiFi
  Password: MyPassword123
  Security: WPA2PSK / AES
  Channel: 0 (auto)

You: /block aa:bb:cc:dd:ee:ff
Bot: Device aa:bb:cc:dd:ee:ff has been blocked from internet access

You: /wan
Bot: WAN / Internet Status
  Internet | 97.62.x.x | PPPoE | NAT: Enable
```

Automatic notification when a device connects:

```
Bot: New Device Connected

  iPhone
  IP: 192.168.1.x
  MAC: aa:bb:cc:dd:ee:ff
  Signal: -46 dBm (Excellent)
  SSID: Home WiFi
```

## Commands

### Device Monitoring

| Command | Description |
|---------|-------------|
| `/devices` | List all connected WiFi devices with hostnames, IPs, and signal |
| `/status` | Device list and monitor status |
| `/refresh` | Poll the router immediately |

### WiFi Management

| Command | Description |
|---------|-------------|
| `/wifi` | Show 2.4GHz WiFi SSID, password, security, channel |
| `/wifi5g` | Show 5GHz WiFi settings |
| `/wifi_name <name>` | Change WiFi SSID name |
| `/wifi_password <pass>` | Change WiFi password |

### Parental Control & Device Blocking

| Command | Description |
|---------|-------------|
| `/block <MAC>` | Block a device from internet access |
| `/unblock <#>` | Unblock a device by rule number |
| `/blocked` | List all blocked devices |

### Firewall

| Command | Description |
|---------|-------------|
| `/firewall` | Show firewall status |
| `/firewall_on` | Enable firewall |
| `/firewall_off` | Disable firewall |

### Network & WAN

| Command | Description |
|---------|-------------|
| `/ports` | List port forwarding rules |
| `/wan` | Show public IP and WAN connection info |

### System

| Command | Description |
|---------|-------------|
| `/reboot` | Start router reboot (asks for confirmation) |
| `/confirm_reboot` | Confirm and execute reboot |
| `/name <MAC> <name>` | Assign a custom name to a device |
| `/names` | List all custom device names |
| `/help` | Show all commands |

## Use Cases

- **Monitor who's on your WiFi** — Get a Telegram notification the moment a new device connects to your Airtel Fiber router
- **Parental controls** — Block a device from the internet without logging into the router dashboard
- **Guest WiFi** — Change your WiFi password before guests arrive, change it back after they leave
- **Remote reboot** — Restart your Airtel router from anywhere when your internet drops
- **Network audit** — Check your public IP, review port forwarding rules, verify firewall status
- **Airtel Xstream Fiber management** — Full router control without the sluggish web interface

## Supported Routers

This tool is designed for Airtel Xstream Fiber routers with Zyxel-based ONT firmware, commonly provided with Airtel Fiber broadband connections in India. The router endpoints are specific to this firmware family. If you have a different Airtel router (e.g., Nokia, Tenda, ASKEY), the CGI endpoints will differ but the overall architecture (MD5 auth → session cookie → CGI POST) is similar.

## Configuration

```json
{
    "router": {
        "ip": "192.168.1.1",
        "username": "admin",
        "password": "YOUR_ROUTER_PASSWORD"
    },
    "telegram": {
        "bot_token": "YOUR_BOT_TOKEN",
        "chat_id": "YOUR_CHAT_ID"
    },
    "notify_on_connect": true,
    "notify_on_disconnect": false,
    "poll_interval_seconds": 60,
    "device_names": {}
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `poll_interval_seconds` | 60 | How often to check for devices |
| `notify_on_connect` | true | Alert when a device connects |
| `notify_on_disconnect` | false | Alert when a device disconnects |
| `send_startup_message` | true | Summary message on start |
| `oui_lookup_enabled` | true | MAC manufacturer lookup |
| `device_names` | `{}` | Custom MAC-to-name map |

## Run as a Background Service

### macOS (LaunchAgent)

```bash
cat > ~/Library/LaunchAgents/com.router-monitor.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.router-monitor</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>REPO_PATH/router_monitor.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>REPO_PATH</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

# Replace REPO_PATH with your path, then:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.router-monitor.plist
```

### Linux (systemd)

```bash
sudo tee /etc/systemd/system/router-monitor.service << 'EOF'
[Unit]
Description=Airtel Router Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/airtel-router-monitor
ExecStart=/usr/bin/python3 /home/YOUR_USERNAME/airtel-router-monitor/router_monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now router-monitor
```

### Windows (Task Scheduler)

Create `run.vbs` in the repo folder:
```vbs
Set objShell = CreateObject("WScript.Shell")
objShell.Run "cmd /c python router_monitor.py", 0, False
```

Schedule it:
```bat
schtasks /create /tn "RouterMonitor" /tr "wscript.exe run.vbs" /sc onlogon /f
```

### Android (Termux)

```bash
pkg install python
pip install requests urllib3
python router_monitor.py
```
Install Termux:Boot for auto-start on boot. Run `termux-wake-lock` to keep the process alive.

### Docker

```bash
docker compose up -d
```

## How It Works

### Authentication

The Airtel Fiber router uses an MD5 challenge-response login:

1. `GET /cgi-bin/login_advance.cgi` returns a per-session challenge (`sid`)
2. Client computes `MD5(urlencode(password) + ":" + sid)`
3. POST the hash to receive a `COOKIE_SESSION_KEY` session cookie
4. All subsequent requests use this cookie

### Device Discovery

The tool uses the router's EasyMesh AP Details endpoint to get real device hostnames:

1. `GET /cgi-bin/EasyMeshTopology.cgi` — find mesh AP MAC addresses
2. `GET /cgi-bin/EasyMeshAPDetails.cgi?APMACDetails=<mac>` — returns host name, IP, MAC, SSID, RSSI

Falls back to WLAN station-info endpoints (MAC only, no hostnames) if EasyMesh is unavailable.

### Endpoints

| Feature | Router Endpoint |
|---------|----------------|
| Device discovery | `EasyMeshAPDetails.cgi` |
| WiFi 2.4GHz settings | `wlan_general.cgi` |
| WiFi 5GHz settings | `wlan5_general.cgi` |
| Parental control | `ParentalControl.cgi`, `PCP_list.cgi` |
| Firewall | `TELFirewall_general.cgi` |
| Port forwarding | `port_forwarding_list.cgi` |
| WAN status | `broadband.cgi` |
| Router reboot | `reboot.cgi` |

### Device Name Resolution

1. Custom name (set via `/name` command or config file)
2. Router hostname (from EasyMesh AP Details)
3. Manufacturer (from MAC OUI lookup via macvendors.com)
4. MAC address (fallback)

## FAQ

**Can I use this with any Airtel router?**

This tool is built for Airtel Xstream Fiber routers with Zyxel-based ONT firmware. If your Airtel router has a different firmware (Nokia, Tenda, ASKEY), the CGI endpoints will be different. The authentication mechanism and overall architecture are similar across Airtel Fiber routers, so you can adapt the endpoints.

**Does the tool need an internet connection?**

It needs internet access to communicate with the Telegram API (for sending/receiving bot messages). The router communication happens over your local network. If your internet is down, the bot cannot send Telegram messages, but you can still run it locally with `--once` to check devices.

**Will it work if my Mac/Linux machine goes to sleep?**

No. The machine running the bot must stay awake. On macOS, use `caffeinate` or prevent sleep in System Settings. On Linux, the systemd service keeps it running. On Android, use `termux-wake-lock`.

**Can multiple people use the same bot?**

The bot only responds to the configured `chat_id` in the config file. Only the person whose Telegram chat ID is in the config can send commands. You can share the bot's token with family members, but they would need to be added to the config.

**Is my router password stored securely?**

The password is stored in plain text in `router_monitor_config.json` on the machine running the bot. This file is in `.gitignore` and will not be committed. For better security, you can modify the script to read from environment variables or a secrets manager.

**Does changing WiFi settings reboot the router?**

No. The router applies WiFi changes (SSID, password) without a reboot. Devices will need to reconnect with the new credentials. Band steering syncs changes between 2.4GHz and 5GHz automatically.

**How do I find my router's MAC address for blocking?**

Send `/devices` to the bot — it lists all connected devices with their MAC addresses. You can then use `/block <MAC>` to block any of them.

## Limitations

- WiFi devices only. The router does not expose wired/LAN device hostnames through its web interface.
- The machine running the bot must stay on. If it sleeps or shuts down, monitoring pauses.
- Designed for Airtel Fiber routers with Zyxel-based firmware. Other router models may need endpoint changes.
- The router allows one active admin session. Concurrent logins from the bot and a browser may invalidate each other.

## Repository Structure

```
airtel-router-monitor/
├── router_monitor.py                  Application (bot + monitor)
├── requirements.txt                   Python dependencies
├── router_monitor_config.example.json Configuration template
├── Dockerfile                         Container image
├── docker-compose.yml                 Docker Compose config
├── .gitignore
├── LICENSE                            MIT
└── README.md
```

## Contributing

Pull requests welcome. If you've adapted this for a different Airtel router model (Nokia, Tenda, ASKEY) or added features (Docker support, Home Assistant integration, REST API), please open a PR.

## License

MIT — see [LICENSE](LICENSE).
