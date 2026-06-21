# deploy/ — field provisioning helpers

## Field Wi-Fi via a file on the card (`wifi-from-bootfs`)

Set Wi-Fi at a new site **without SSH or a monitor**, by dropping a text file on
the SSD's FAT `bootfs` partition (readable/writable from Windows, macOS, Linux).

### Install once (while you have access to the Pi)

```bash
sudo install -m 755 deploy/wifi-from-bootfs.sh /usr/local/sbin/wifi-from-bootfs.sh
sudo install -m 644 deploy/wifi-from-bootfs.service /etc/systemd/system/wifi-from-bootfs.service
sudo systemctl enable wifi-from-bootfs.service
```

### Use it in the field

1. Plug the SSD into a computer → open the small **`bootfs`** drive.
2. Create a file named **`wifi.txt`** there:
   ```
   ssid=SiteB-Network
   password=SiteB-password
   country=BR
   ```
3. Eject, reinsert into the Pi, power on. On boot it joins the network and then
   **deletes `wifi.txt`** (so the password isn't left in plaintext on the card).

Notes:
- Windows line endings are handled. Keep `wifi.txt` plain text.
- The created NetworkManager profile is named `field-<ssid>` and autoconnects.
- Existing saved networks are kept; it just adds/overwrites the `field-*` one.

## Alternative: no card pulling at all — captive-portal AP fallback

For fully phone-based field setup (no laptop, no card removal), install
**Comitup** or **RaspAP**: when the Pi finds no known Wi-Fi, it broadcasts its own
access point; connect a phone to it and pick the local network + enter the
password via a captive portal. More setup, but the most hands-off in the field.
