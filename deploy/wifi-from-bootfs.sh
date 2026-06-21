#!/bin/bash
# Apply Wi-Fi credentials dropped on the FAT boot partition (/boot/firmware),
# which is editable from any OS — including Windows. Lets you set Wi-Fi in the
# field by dropping a text file on the SSD: no SSH, no monitor, no ext4 access.
#
# wifi.txt format (one key per line):
#   ssid=MyNetwork
#   password=MySecret
#   country=BR        # optional but recommended (regulatory domain / 5GHz)
#
# The file is DELETED after a successful apply so the password isn't left in
# plaintext on the card.
set -euo pipefail

CRED="/boot/firmware/wifi.txt"
[ -f "$CRED" ] || exit 0

val() { grep -i "^$1=" "$CRED" | head -n1 | cut -d= -f2- | tr -d '\r' | xargs; }

ssid="$(val ssid)"
pass="$(val password)"
country="$(val country)"

[ -n "$ssid" ] || { echo "wifi-from-bootfs: no ssid in $CRED"; exit 0; }

if [ -n "$country" ]; then
    raspi-config nonint do_wifi_country "$country" || true
fi

con="field-$ssid"
nmcli connection delete "$con" >/dev/null 2>&1 || true
nmcli connection add type wifi con-name "$con" ssid "$ssid"
nmcli connection modify "$con" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$pass" \
    connection.autoconnect yes \
    connection.autoconnect-priority 10
nmcli connection up "$con" || true

echo "wifi-from-bootfs: applied network '$ssid'"
rm -f "$CRED"
