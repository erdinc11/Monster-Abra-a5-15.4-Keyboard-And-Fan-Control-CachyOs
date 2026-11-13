#!/bin/bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Kurulum yönetici yetkisiyle çalıştırılmalı." >&2
    exit 1
fi

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-}"
if [[ -z "$TARGET_USER" || "$TARGET_USER" == root ]]; then
    echo "Hedef kullanıcı belirlenemedi; sudo ile oturum açmış kullanıcı olarak çalıştırın." >&2
    exit 1
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
TARGET_GROUP="$(id -gn "$TARGET_USER")"
KERNEL="$(uname -r)"

for file in abra_control.py abra-control-fan abra-control-permissions.service 99-abra-control.rules driver/ite_8291.c driver/Kbuild driver/dkms.conf; do
    [[ -f "$APP_DIR/$file" ]] || { echo "Dosya bulunamadı: $APP_DIR/$file" >&2; exit 1; }
done

if ! command -v dkms >/dev/null || ! command -v clang >/dev/null; then
    echo "DKMS ve clang gerekli; CachyOS paketlerinden dkms ve clang kurun." >&2
    exit 1
fi
if ! test -e "/lib/modules/$KERNEL/build/Makefile"; then
    echo "Kernel $KERNEL için başlıklar kurulu değil." >&2
    exit 1
fi
if ! modinfo acpi_call >/dev/null 2>&1; then
    echo "acpi_call modülü kurulu değil; CachyOS acpi_call-dkms paketini kurun." >&2
    exit 1
fi

DKMS_NAME=abra_ite8291
DKMS_VERSION=1.1
install -d -m 0755 "/usr/src/${DKMS_NAME}-${DKMS_VERSION}"
install -m 0644 "$APP_DIR/driver/ite_8291.c" "$APP_DIR/driver/Kbuild" "$APP_DIR/driver/dkms.conf" "/usr/src/${DKMS_NAME}-${DKMS_VERSION}/"
if dkms status -m "$DKMS_NAME" -v 1.0 2>/dev/null | grep -q "$DKMS_NAME/1.0"; then
    dkms remove -m "$DKMS_NAME" -v 1.0 --all
fi
if ! dkms status -m "$DKMS_NAME" -v "$DKMS_VERSION" 2>/dev/null | grep -q "$DKMS_NAME/$DKMS_VERSION"; then
    dkms add -m "$DKMS_NAME" -v "$DKMS_VERSION"
fi
if ! dkms status -m "$DKMS_NAME" -v "$DKMS_VERSION" -k "$KERNEL" 2>/dev/null | grep -q ': installed'; then
    dkms build -m "$DKMS_NAME" -v "$DKMS_VERSION" -k "$KERNEL"
    dkms install -m "$DKMS_NAME" -v "$DKMS_VERSION" -k "$KERNEL"
fi
depmod -a "$KERNEL"

cat > /etc/modules-load.d/abra-control.conf <<'EOF'
acpi_call
ite_8291
EOF
modprobe acpi_call
modprobe ite_8291

install -D -m 0755 "$APP_DIR/abra-control-fan" /usr/local/libexec/abra-control-fan
install -D -m 0755 "$APP_DIR/abra-control-permissions" /usr/local/libexec/abra-control-permissions
install -D -m 0644 "$APP_DIR/abra-control-permissions.service" /etc/systemd/system/abra-control-permissions.service
install -D -m 0644 "$APP_DIR/99-abra-control.rules" /etc/udev/rules.d/99-abra-control.rules

if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx video; then
    usermod -aG video "$TARGET_USER"
    echo "$TARGET_USER video grubuna eklendi; değişikliğin uygulanması için oturumu kapatıp açın."
fi

SUDOERS_FILE="/etc/sudoers.d/abra-control"
printf '%s ALL=(root) NOPASSWD: /usr/local/libexec/abra-control-fan ""\n' "$TARGET_USER" > "$SUDOERS_FILE"
chmod 0440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE"

systemctl daemon-reload
udevadm control --reload-rules
systemctl enable --now abra-control-permissions.service
udevadm trigger --subsystem-match=leds
systemctl restart abra-control-permissions.service

DESKTOP_DIR="$TARGET_HOME/.local/share/applications"
install -d -o "$TARGET_USER" -g "$TARGET_GROUP" -m 0755 "$DESKTOP_DIR"
DESKTOP_FILE="$DESKTOP_DIR/abra-control.desktop"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Abra Kontrol
Comment=Klavye RGB ve fan ayarları
Exec=/usr/bin/python3 "$APP_DIR/abra_control.py"
TryExec=/usr/bin/python3
Icon=input-keyboard
Terminal=false
Categories=Settings;HardwareSettings;
EOF
chown "$TARGET_USER:$TARGET_GROUP" "$DESKTOP_FILE"
chmod 0644 "$DESKTOP_FILE"

echo "Abra Kontrol kuruldu. Menüden 'Abra Kontrol' uygulamasını açabilirsiniz."
