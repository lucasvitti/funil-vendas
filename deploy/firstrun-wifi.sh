#!/bin/bash
# One-shot Wi-Fi provisioning via the boot partition — no network/SSH/monitor needed.
#
# HOW TO USE (all from your laptop, card in a USB reader):
#   1. Copy THIS file onto the FAT "bootfs" partition, renamed to:  firstrun.sh
#   2. Edit the two values below (your phone hotspot SSID + password).
#   3. Open cmdline.txt on that same partition (back it up first) and append — on
#      the SAME single line, with a leading space — exactly:
#        systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target
#   4. Safely eject, put the card back in the Pi, power on.
# On boot it writes a NetworkManager connection for the hotspot, cleans up, reboots,
# and joins the hotspot. Then reach it over the hotspot (mDNS `ssh pi-cam` or its IP).
set +e

# ===== EDIT THESE TWO =====
WIFI_SSID="YOUR_PHONE_HOTSPOT"
WIFI_PASS="YOUR_HOTSPOT_PASSWORD"
# ==========================

UUID=$(cat /proc/sys/kernel/random/uuid)
CONN="/etc/NetworkManager/system-connections/field-wifi.nmconnection"
cat > "$CONN" <<EOF
[connection]
id=field-wifi
uuid=$UUID
type=wifi
autoconnect=true
autoconnect-priority=30
[wifi]
mode=infrastructure
ssid=$WIFI_SSID
[wifi-security]
key-mgmt=wpa-psk
psk=$WIFI_PASS
[ipv4]
method=auto
[ipv6]
method=auto
EOF
chmod 600 "$CONN"
chown root:root "$CONN"

# Remove the one-shot hook from cmdline.txt and self-delete (so it runs only once).
sed -i 's# systemd.run=/boot/firmware/firstrun.sh##; s# systemd.run_success_action=reboot##; s# systemd.unit=kernel-command-line.target##' /boot/firmware/cmdline.txt
rm -f /boot/firmware/firstrun.sh
exit 0
