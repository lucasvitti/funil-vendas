#!/bin/bash
# add-wifi <name> <ssid> <password> [priority]
# Add (or replace) a saved Wi-Fi network. NetworkManager auto-connects to
# whichever saved network is in range; higher priority wins ties.
#
#   add-wifi home    "MyHomeWiFi"   "homepass"
#   add-wifi hotspot "LucasPhone"   "hotpass"   30      # anchor: high priority
#   add-wifi office  "CorpWiFi"     "officepass"
#
# List saved:   nmcli connection show
# Remove one:   sudo nmcli connection delete <name>
set -e
if [ $# -lt 3 ]; then
    echo "usage: add-wifi <name> <ssid> <password> [priority]"
    exit 1
fi
NAME="$1"; SSID="$2"; PASS="$3"; PRIO="${4:-10}"

sudo nmcli connection delete "$NAME" >/dev/null 2>&1 || true
sudo nmcli connection add type wifi con-name "$NAME" ssid "$SSID"
sudo nmcli connection modify "$NAME" \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS" \
    connection.autoconnect yes connection.autoconnect-priority "$PRIO"

echo "saved '$NAME' (ssid=$SSID, priority=$PRIO)."
echo "--- all saved Wi-Fi networks ---"
nmcli -t -f NAME,TYPE connection show | awk -F: '$2 ~ /wireless/ {print "  "$1}'
