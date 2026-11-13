#!/usr/bin/env python3
"""Abra A5 V15.4 control panel for the ITE 8291 keyboard and Tongfang EC."""

from __future__ import annotations

import colorsys
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk


APP_NAME = "Abra Kontrol"
APP_ID = "tr.monster.abra-control"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "abra-control"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
AUTOSTART_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "abra-control.desktop"
LED_GLOB = "/sys/class/leds/rgb:kbd_backlight*"
FAN_HELPER = Path("/usr/local/libexec/abra-control-fan")
FAN_MODES = {
    "Otomatik": 0x00,
    "Turbo": 0x10,
    "Boost": 0x40,
}
MODE_NAMES = {
    0x00: "Otomatik", 0x10: "Turbo", 0x40: "Boost", 0xA0: "Özel mod",
    0x81: "Sabit L1", 0x82: "Sabit L2", 0x83: "Sabit L3",
}


class StatusNotifierTray:
    """A small StatusNotifierItem with a DBusMenu, understood by KDE's tray."""

    ITEM_XML = """<node>
      <interface name="org.kde.StatusNotifierItem">
        <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
        <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
        <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
        <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
        <property name="Category" type="s" access="read"/>
        <property name="Id" type="s" access="read"/>
        <property name="Title" type="s" access="read"/>
        <property name="Status" type="s" access="read"/>
        <property name="WindowId" type="u" access="read"/>
        <property name="IconName" type="s" access="read"/>
        <property name="IconPixmap" type="a(iiay)" access="read"/>
        <property name="OverlayIconName" type="s" access="read"/>
        <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
        <property name="AttentionIconName" type="s" access="read"/>
        <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
        <property name="AttentionMovieName" type="s" access="read"/>
        <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
        <property name="ItemIsMenu" type="b" access="read"/>
        <property name="Menu" type="o" access="read"/>
      </interface>
    </node>"""
    FDO_ITEM_XML = ITEM_XML.replace("org.kde.StatusNotifierItem", "org.freedesktop.StatusNotifierItem")

    MENU_XML = """<node>
      <interface name="com.canonical.dbusmenu">
        <method name="GetLayout">
          <arg name="parentId" type="i" direction="in"/>
          <arg name="recursionDepth" type="i" direction="in"/>
          <arg name="propertyNames" type="as" direction="in"/>
          <arg name="revision" type="u" direction="out"/>
          <arg name="layout" type="(ia{sv}av)" direction="out"/>
        </method>
        <method name="GetGroupProperties">
          <arg name="ids" type="ai" direction="in"/>
          <arg name="propertyNames" type="as" direction="in"/>
          <arg name="properties" type="a(ia{sv})" direction="out"/>
        </method>
        <method name="GetProperty">
          <arg name="id" type="i" direction="in"/>
          <arg name="name" type="s" direction="in"/>
          <arg name="value" type="v" direction="out"/>
        </method>
        <method name="Event">
          <arg name="id" type="i" direction="in"/>
          <arg name="eventId" type="s" direction="in"/>
          <arg name="data" type="v" direction="in"/>
          <arg name="timestamp" type="u" direction="in"/>
        </method>
        <method name="EventGroup">
          <arg name="events" type="a(isvu)" direction="in"/>
          <arg name="idErrors" type="ai" direction="out"/>
        </method>
        <method name="AboutToShow">
          <arg name="id" type="i" direction="in"/>
          <arg name="needUpdate" type="b" direction="out"/>
        </method>
        <property name="Version" type="u" access="read"/>
        <property name="TextDirection" type="s" access="read"/>
        <property name="Status" type="s" access="read"/>
        <property name="IconThemePath" type="as" access="read"/>
      </interface>
    </node>"""

    def __init__(self, app):
        self.app = app
        self.connection = None
        self.registered = False
        self.error = None
        self._watcher = 0
        self.service_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        try:
            self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            item_info = Gio.DBusNodeInfo.new_for_xml(self.ITEM_XML).interfaces[0]
            fdo_item_info = Gio.DBusNodeInfo.new_for_xml(self.FDO_ITEM_XML).interfaces[0]
            menu_info = Gio.DBusNodeInfo.new_for_xml(self.MENU_XML).interfaces[0]
            self.connection.register_object(
                "/StatusNotifierItem", item_info,
                self._on_item_method, self._get_item_property, None,
            )
            self.connection.register_object(
                "/StatusNotifierItem", fdo_item_info,
                self._on_item_method, self._get_item_property, None,
            )
            self.connection.register_object(
                "/Menu", menu_info,
                self._on_menu_method, self._get_menu_property, None,
            )
            self._watcher = Gio.bus_watch_name_on_connection(
                self.connection, "org.kde.StatusNotifierWatcher",
                Gio.BusNameWatcherFlags.NONE, self._watcher_appeared,
                self._watcher_vanished,
            )
            try:
                owner = self.connection.call_sync(
                    "org.freedesktop.DBus", "/org/freedesktop/DBus",
                    "org.freedesktop.DBus", "GetNameOwner",
                    GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
                    GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 1000, None,
                ).unpack()[0]
                self._watcher_appeared(self.connection, "org.kde.StatusNotifierWatcher", owner)
            except Exception as exc:
                self.error = f"KDE sistem tepsisi bulunamadı: {exc}"
            GLib.timeout_add_seconds(2, self._retry_registration)
        except Exception as exc:
            self.error = f"Sistem tepsisi başlatılamadı: {exc}"
            self.app.set_status(self.error)

    def _watcher_appeared(self, _connection, _name, _owner, _data=None):
        if self.registered:
            return
        try:
            name_result = self.connection.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "RequestName",
                GLib.Variant("(su)", (self.service_name, 4)),
                GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 1000, None,
            ).unpack()[0]
            if name_result not in (1, 4):
                raise RuntimeError("Tepsi simgesi için D-Bus adı alınamadı")
            self.connection.call_sync(
                "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self.service_name,)),
                None, Gio.DBusCallFlags.NONE, 3000, None,
            )
            self.registered = True
            self.error = None
        except Exception as exc:
            self.registered = False
            self.error = f"Sistem tepsisine kaydolunamadı: {exc}"
            self.app.set_status(self.error)

    def _watcher_vanished(self, *_):
        self.registered = False
        self.error = "KDE sistem tepsisi şu anda kullanılamıyor."

    def _retry_registration(self):
        if self.registered or not self.connection:
            return GLib.SOURCE_REMOVE
        try:
            owner = self.connection.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "GetNameOwner",
                GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
                GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 1000, None,
            ).unpack()[0]
            self._watcher_appeared(self.connection, "org.kde.StatusNotifierWatcher", owner)
        except Exception as exc:
            self.error = f"KDE sistem tepsisine bağlanılamadı: {exc}"
            self.app.set_status(self.error)
        return GLib.SOURCE_CONTINUE if not self.registered else GLib.SOURCE_REMOVE

    @staticmethod
    def _variant(signature, value):
        return GLib.Variant(signature, value)

    def _get_item_property(self, _connection, _sender, _path, _interface, name):
        values = {
            "Category": self._variant("s", "ApplicationStatus"),
            "Id": self._variant("s", "abra-control"),
            "Title": self._variant("s", APP_NAME),
            "Status": self._variant("s", "Active"),
            "WindowId": self._variant("u", 0),
            "IconName": self._variant("s", "input-keyboard-symbolic"),
            "IconPixmap": self._variant("a(iiay)", []),
            "OverlayIconName": self._variant("s", ""),
            "OverlayIconPixmap": self._variant("a(iiay)", []),
            "AttentionIconName": self._variant("s", ""),
            "AttentionIconPixmap": self._variant("a(iiay)", []),
            "AttentionMovieName": self._variant("s", ""),
            "ToolTip": self._variant(
                "(sa(iiay)ss)",
                ("input-keyboard-symbolic", [], APP_NAME, "Klavye RGB ve fan denetimi"),
            ),
            "ItemIsMenu": self._variant("b", False),
            "Menu": self._variant("o", "/Menu"),
        }
        return values.get(name)

    def _get_menu_property(self, _connection, _sender, _path, _interface, name):
        values = {
            "Version": self._variant("u", 3),
            "TextDirection": self._variant("s", "ltr"),
            "Status": self._variant("s", "normal"),
            "IconThemePath": self._variant("as", []),
        }
        return values.get(name)

    def _menu_item(self, item_id: int, label: str):
        properties = {
            "label": self._variant("s", label),
            "enabled": self._variant("b", True),
            "visible": self._variant("b", True),
        }
        return self._variant("(ia{sv}av)", (item_id, properties, []))

    def _menu_properties(self, item_id: int):
        labels = {1: "Abra Kontrol'ü aç", 2: "Kapat"}
        return {
            "label": self._variant("s", labels[item_id]),
            "enabled": self._variant("b", True),
            "visible": self._variant("b", True),
        }

    def _menu_layout(self):
        children = [
            self._menu_item(1, "Abra Kontrol'ü aç"),
            self._menu_item(2, "Kapat"),
        ]
        root = (0, {}, children)
        return self._variant("(u(ia{sv}av))", (1, root))

    def _activate_menu_item(self, item_id: int):
        if item_id == 1:
            self.app.show_window()
        elif item_id == 2:
            self.app.quit()

    def _on_item_method(self, _connection, _sender, _path, _interface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self.app.show_window()
        invocation.return_value(self._variant("()", ()))

    def _on_menu_method(self, _connection, _sender, _path, _interface, method, params, invocation):
        args = params.unpack()
        if method == "GetLayout":
            invocation.return_value(self._menu_layout())
        elif method == "GetGroupProperties":
            ids = args[0]
            result = [(item_id, self._menu_properties(item_id)) for item_id in ids if item_id in (1, 2)]
            invocation.return_value(self._variant("(a(ia{sv}))", (result,)))
        elif method == "GetProperty":
            item_id, name = args
            if item_id in (1, 2) and name == "label":
                label = "Abra Kontrol'ü aç" if item_id == 1 else "Kapat"
                value = self._variant("s", label)
                invocation.return_value(self._variant("(v)", (value,)))
            elif item_id in (1, 2) and name in ("enabled", "visible"):
                invocation.return_value(self._variant("(v)", (self._variant("b", True),)))
            else:
                invocation.return_error_literal(Gio.DBusError, Gio.DBusError.UNKNOWN_PROPERTY, "Menü özelliği bulunamadı")
        elif method == "Event":
            item_id, event_name, _data, _timestamp = args
            if event_name == "clicked":
                self._activate_menu_item(item_id)
            invocation.return_value(self._variant("()", ()))
        elif method == "EventGroup":
            events = args[0]
            errors = []
            for item_id, event_name, _data, _timestamp in events:
                if item_id not in (1, 2):
                    errors.append(item_id)
                elif event_name == "clicked":
                    self._activate_menu_item(item_id)
            invocation.return_value(self._variant("(ai)", (errors,)))
        elif method == "AboutToShow":
            invocation.return_value(self._variant("(b)", (False,)))


class Hardware:
    """Small sysfs/ACPI adapter for the interfaces verified on this machine."""

    def __init__(self):
        self.led_paths = self._find_leds()

    @staticmethod
    def _find_leds() -> list[Path]:
        paths = [Path(p) for p in glob.glob(LED_GLOB)]
        return sorted(paths, key=lambda p: 0 if p.name == "rgb:kbd_backlight" else int(p.name.rsplit("_", 1)[1]))

    @property
    def keyboard_ready(self) -> bool:
        return len(self.led_paths) == 4 and all((p / "multi_intensity").exists() for p in self.led_paths)

    def read_led(self, zone: int, attr: str) -> str:
        return (self.led_paths[zone] / attr).read_text().strip()

    def write_led(self, zone: int, attr: str, value: str):
        (self.led_paths[zone] / attr).write_text(value)

    @staticmethod
    def fan_request(request: dict) -> dict:
        if not FAN_HELPER.exists():
            raise RuntimeError("Fan bileşeni kurulu değil. setup.sh dosyasını çalıştırın.")
        try:
            result = subprocess.run(
                ["sudo", "-n", str(FAN_HELPER)], input=json.dumps(request),
                check=True, capture_output=True, text=True, timeout=5,
            )
            response = json.loads(result.stdout)
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip()
            if not detail and exc.stdout:
                try:
                    detail = json.loads(exc.stdout).get("error", "")
                except (ValueError, AttributeError):
                    pass
            detail = detail or "Fan denetim aracı başarısız oldu"
            raise RuntimeError(detail) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Fan denetleyicisi yanıt vermedi") from exc
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "Fan işlemi başarısız oldu"))
        return response

    def get_fan_mode(self) -> int:
        return self.fan_request({"op": "status"})["mode"]

    def set_fan_mode(self, mode: int):
        mode_names = {0x00: "auto", 0x10: "turbo", 0x40: "boost"}
        if mode not in mode_names:
            raise ValueError("Geçersiz fan modu")
        result = self.fan_request({"op": "mode", "mode": mode_names[mode]})
        if result["mode"] != mode:
            raise RuntimeError("EC fan modu değişikliğini doğrulamadı")

    def set_fixed_fan_level(self, level: int):
        if not 1 <= level <= 3:
            raise ValueError("Sabit fan seviyesi L1–L3 arasında olmalı")
        result = self.fan_request({"op": "level", "level": level})
        if result.get("mode") != 0x80 + level:
            raise RuntimeError("EC sabit fan seviyesini doğrulamadı")

    @staticmethod
    def get_gpu_temperature() -> int | None:
        gpu = None
        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi:
            try:
                result = subprocess.run(
                    [nvidia_smi, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                    check=True, capture_output=True, text=True, timeout=2,
                )
                gpu = int(result.stdout.strip().splitlines()[0])
            except Exception:
                pass
        return gpu


class AbraControl(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.hw = Hardware()
        self.settings = self.load_settings()
        self._settings_loaded = SETTINGS_PATH.exists()
        self._saved_snapshot = None
        self.start_minimized = "--start-minimized" in sys.argv[1:]
        self._first_activation = True
        self._loading_settings = False
        self.window = None
        self.tray = None
        self.status = None
        self.status_icon = None
        self.device_badge = None
        self.stack = None
        self.zone_buttons = []
        self.zone_selection = int(self.settings["selected_zone"])
        self.selected_rgb = tuple(self.settings["selected_rgb"])
        self.color_channel_spins = []
        self.color_preview = None
        self._syncing_color = False
        self.brightness_scale = None
        self.brightness_value = None
        self._syncing_brightness = False
        self.speed_scale = None
        self.speed_value = None
        self.effect_dropdown = None
        self.animation_id = None
        self.animation_start = 0.0
        self.animation_last = 0.0
        self.saved_colors = [tuple(color) for color in self.settings["zone_colors"]]
        self.fan_mode_label = None
        self.cpu_temp_value = None
        self.gpu_temp_value = None
        self.cpu_fan_value = None
        self.gpu_fan_value = None
        self.fan_mode_buttons = {}
        self.fan_manual_scale = None
        self.fan_manual_value = None
        self.fan_manual_button = None
        self.restore_check = None
        self.autostart_check = None
        self._autostart_handler = None
        self._closing = False
        self.connect("shutdown", self.on_shutdown)

    @staticmethod
    def _default_settings() -> dict:
        return {
            "version": 1,
            "auto_start": False,
            "selected_zone": 0,
            "selected_rgb": [10, 38, 50],
            "zone_colors": [[50, 50, 50] for _ in range(4)],
            "zone_brightness": [50, 50, 50, 50],
            "effect": 0,
            "speed": 5,
            "animation_running": False,
            "fan_mode": 0x00,
            "fan_manual_level": 3,
            "restore_fan_on_exit": True,
            "active_page": "keyboard",
        }

    @staticmethod
    def load_settings() -> dict:
        settings = AbraControl._default_settings()
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                settings.update(saved)
        except (OSError, ValueError, TypeError):
            pass

        def number(value, default, low, high):
            try:
                return max(low, min(high, int(value)))
            except (TypeError, ValueError):
                return default

        settings["auto_start"] = bool(settings.get("auto_start"))
        settings["selected_zone"] = number(settings.get("selected_zone"), 0, 0, 4)
        selected_rgb = settings.get("selected_rgb", [10, 38, 50])
        if not isinstance(selected_rgb, list):
            selected_rgb = [10, 38, 50]
        settings["selected_rgb"] = [number(v, 0, 0, 50) for v in selected_rgb[:3]]
        if len(settings["selected_rgb"]) != 3:
            settings["selected_rgb"] = [10, 38, 50]

        def colors(value):
            result = []
            for color in value[:4] if isinstance(value, list) else []:
                if not isinstance(color, list) or len(color) != 3:
                    continue
                result.append([number(v, 0, 0, 50) for v in color])
            while len(result) < 4:
                result.append([50, 50, 50])
            return result[:4]

        settings["zone_colors"] = colors(settings.get("zone_colors", []))
        brightness = settings.get("zone_brightness", [])
        settings["zone_brightness"] = [
            number(brightness[i], 50, 0, 50) if isinstance(brightness, list) and len(brightness) > i else 50
            for i in range(4)
        ]
        settings["effect"] = number(settings.get("effect"), 0, 0, 3)
        settings["speed"] = number(settings.get("speed"), 5, 1, 10)
        settings["animation_running"] = bool(settings.get("animation_running"))
        allowed_fan_modes = {0x00, 0x10, 0x40, 0x81, 0x82, 0x83}
        try:
            fan_mode = int(settings.get("fan_mode", 0))
        except (TypeError, ValueError):
            fan_mode = 0
        settings["fan_mode"] = fan_mode if fan_mode in allowed_fan_modes else 0x00
        settings["fan_manual_level"] = number(settings.get("fan_manual_level"), 3, 1, 3)
        settings["restore_fan_on_exit"] = bool(settings.get("restore_fan_on_exit", True))
        settings["active_page"] = settings.get("active_page") if settings.get("active_page") in ("keyboard", "fans") else "keyboard"
        settings["version"] = 1
        return settings

    def save_settings(self):
        if self._loading_settings:
            return
        if self.zone_buttons:
            self.settings["selected_zone"] = self.zone_selection
        if self.color_channel_spins and len(self.color_channel_spins) == 3:
            self.settings["selected_rgb"] = list(self.selected_rgb)
        if self.effect_dropdown:
            self.settings["effect"] = self.effect_dropdown.get_selected()
        if self.speed_scale:
            self.settings["speed"] = round(self.speed_scale.get_value())
        if self.restore_check:
            self.settings["restore_fan_on_exit"] = self.restore_check.get_active()
        if self.fan_manual_scale:
            self.settings["fan_manual_level"] = round(self.fan_manual_scale.get_value())
        if self.stack:
            self.settings["active_page"] = self.stack.get_visible_child_name() or "keyboard"
        payload = json.dumps(self.settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if payload == self._saved_snapshot:
            return
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            temporary_path = SETTINGS_PATH.with_suffix(".json.tmp")
            temporary_path.write_text(payload, encoding="utf-8")
            temporary_path.replace(SETTINGS_PATH)
            self._saved_snapshot = payload
        except OSError as exc:
            self.set_status(f"Ayarlar kaydedilemedi: {exc}")

    @staticmethod
    def _desktop_exec_argument(value: str) -> str:
        escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def sync_autostart(self, enabled: bool):
        if not enabled:
            try:
                AUTOSTART_PATH.unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimeError(f"Otomatik başlatma kaldırılamadı: {exc}") from exc
            return
        AUTOSTART_PATH.parent.mkdir(parents=True, exist_ok=True)
        python_path = shutil.which("python3") or sys.executable
        command = " ".join((
            self._desktop_exec_argument(python_path),
            self._desktop_exec_argument(str(Path(__file__).resolve())),
            "--start-minimized",
        ))
        entry = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Abra Kontrol\n"
            "Comment=Klavye RGB ve fan ayarları\n"
            f"Exec={command}\n"
            "TryExec=/usr/bin/python3\n"
            "Icon=input-keyboard\n"
            "Terminal=false\n"
            "Categories=Settings;HardwareSettings;\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
        AUTOSTART_PATH.write_text(entry, encoding="utf-8")

    def on_autostart_toggled(self, button):
        enabled = button.get_active()
        try:
            self.sync_autostart(enabled)
            self.settings["auto_start"] = enabled
            self.save_settings()
            self.set_status("Sistem ile başlatma açıldı." if enabled else "Sistem ile başlatma kapatıldı.")
        except OSError as exc:
            if self._autostart_handler:
                button.handler_block(self._autostart_handler)
            button.set_active(not enabled)
            if self._autostart_handler:
                button.handler_unblock(self._autostart_handler)
            self.set_status(f"Sistem ile başlatma kaydedilemedi: {exc}")
        except RuntimeError as exc:
            if self._autostart_handler:
                button.handler_block(self._autostart_handler)
            button.set_active(not enabled)
            if self._autostart_handler:
                button.handler_unblock(self._autostart_handler)
            self.set_status(str(exc))

    def show_window(self):
        if self.window:
            self.window.set_visible(True)
            self.window.present()

    def do_activate(self):
        if self.window:
            self.show_window()
            return
        self._loading_settings = True
        self.install_style()
        self.tray = StatusNotifierTray(self)
        self.window = Gtk.ApplicationWindow(application=self, title=APP_NAME)
        self.window.set_default_size(960, 820)
        self.window.set_size_request(700, 580)
        self.window.connect("close-request", self.on_close)

        header = Gtk.HeaderBar()
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title = Gtk.Label(label="Abra Kontrol")
        title.add_css_class("header-title")
        title.set_xalign(0)
        subtitle = Gtk.Label(label="MONSTER ABRA A5 V15.4")
        subtitle.add_css_class("header-subtitle")
        subtitle.set_xalign(0)
        heading.append(title)
        heading.append(subtitle)
        header.set_title_widget(heading)
        device_ready = self.hw.keyboard_ready and FAN_HELPER.exists()
        self.device_badge = Gtk.Label(
            label="● Donanım hazır" if device_ready else "● Denetleyici kontrolü gerekli"
        )
        self.device_badge.add_css_class("device-badge")
        header.pack_end(self.device_badge)
        self.window.set_titlebar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        root.set_margin_top(14)
        root.set_margin_bottom(14)
        root.set_margin_start(22)
        root.set_margin_end(22)
        root.add_css_class("app-shell")
        self.window.set_child(root)

        self.status_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        self.status = Gtk.Label(label="Kontroller hazır.")
        self.status.set_xalign(0)
        self.status.set_hexpand(True)
        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        status_row.add_css_class("status-strip")
        status_row.append(self.status_icon)
        status_row.append(self.status)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(180)
        self.stack.set_vexpand(True)
        self.stack.connect("notify::visible-child-name", lambda *_: self.save_settings())
        self.stack.add_titled(self.wrap_page(self.build_keyboard_page()), "keyboard", "Klavye")
        self.stack.add_titled(self.wrap_page(self.build_fan_page()), "fans", "Fanlar")
        self.stack.set_visible_child_name(self.settings["active_page"])

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        switcher.add_css_class("nav-switcher")
        nav_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        nav_row.set_halign(Gtk.Align.CENTER)
        nav_row.append(switcher)

        root.append(nav_row)
        root.append(self.stack)

        self.autostart_check = Gtk.CheckButton(label="Sistem ile başlat · açılışta sistem tepsisinde başla")
        self.autostart_check.set_active(self.settings["auto_start"])
        self._autostart_handler = self.autostart_check.connect("toggled", self.on_autostart_toggled)
        root.append(self.autostart_check)

        root.append(status_row)
        self._loading_settings = False
        try:
            self.sync_autostart(self.settings["auto_start"])
        except (OSError, RuntimeError) as exc:
            self.set_status(f"Otomatik başlatma ayarı uygulanamadı: {exc}")
        self.restore_saved_fan_mode()
        self.refresh_fan_status()
        self.save_settings()
        if self.settings["animation_running"] and self.hw.keyboard_ready:
            GLib.idle_add(self.start_animation)
        if self.tray and self.tray.error:
            self.set_status(self.tray.error)
        if self.start_minimized and self.tray and self.tray.registered:
            self._first_activation = False
            self.set_status("Abra Kontrol sistem tepsisinde çalışıyor.")
        else:
            self._first_activation = False
            self.window.present()

    def install_style(self):
        css = """
        window { background-color: @theme_bg_color; }
        headerbar { background: transparent; border: none; box-shadow: none; }
        .header-title { font-size: 12pt; font-weight: 700; }
        .header-subtitle { font-size: 8pt; color: @insensitive_fg_color; }
        .device-badge {
            background-color: alpha(@theme_selected_bg_color, 0.14);
            color: @theme_selected_bg_color;
            border-radius: 999px;
            padding: 7px 12px;
            font-size: 9pt;
            font-weight: 700;
        }
        .surface-card {
            background-color: @theme_base_color;
            border: 1px solid alpha(@theme_fg_color, 0.10);
            border-radius: 18px;
            padding: 12px;
        }
        .compact-page .surface-card { padding: 8px; border-radius: 14px; }
        .compact-page .card-title { font-size: 11pt; }
        .compact-page button.zone-chip { border-radius: 9px; padding: 6px 10px; }
        .compact-page button.action-button { border-radius: 9px; padding: 6px 10px; }
        .card-title { font-size: 12pt; font-weight: 700; }
        .card-subtitle { color: @insensitive_fg_color; font-size: 9pt; }
        .section-label { font-size: 10pt; font-weight: 700; }
        .value-label { font-size: 22pt; font-weight: 750; }
        .compact-value { font-size: 16pt; font-weight: 700; }
        .control-value { font-size: 10pt; font-weight: 700; }
        button.zone-chip, button.mode-chip {
            border-radius: 11px;
            padding: 9px 14px;
            font-weight: 650;
        }
        button.zone-chip:checked, button.mode-chip:checked {
            background-color: @theme_selected_bg_color;
            color: @theme_selected_fg_color;
        }
        button.mode-chip { min-width: 112px; min-height: 42px; }
        button.action-button { border-radius: 11px; padding: 9px 15px; font-weight: 650; }
        .status-strip {
            background-color: alpha(@theme_selected_bg_color, 0.10);
            border-radius: 12px;
            padding: 9px 13px;
        }
        .helper-text { color: @insensitive_fg_color; font-size: 9pt; }
        .nav-switcher { padding: 3px; }
        .nav-switcher button { border-radius: 10px; padding: 7px 18px; }
        """
        self.css_provider = Gtk.CssProvider()
        self.css_provider.load_from_data(css.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def wrap_page(self, child: Gtk.Widget) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(child)
        return scroll

    @staticmethod
    def make_card(title: str, subtitle: str | None = None, compact: bool = False):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6 if compact else 9)
        card.add_css_class("surface-card")
        if compact:
            card.add_css_class("compact-card")
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        title_label = Gtk.Label(label=title)
        title_label.set_xalign(0)
        title_label.add_css_class("card-title")
        heading.append(title_label)
        if subtitle:
            subtitle_label = Gtk.Label(label=subtitle)
            subtitle_label.set_xalign(0)
            subtitle_label.set_wrap(True)
            subtitle_label.add_css_class("card-subtitle")
            heading.append(subtitle_label)
        card.append(heading)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6 if compact else 8)
        card.append(body)
        return card, body

    def build_keyboard_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_margin_top(4)
        page.set_margin_bottom(6)
        page.add_css_class("compact-page")

        if not self.hw.keyboard_ready:
            card, body = self.make_card("Klavye denetleyicisi bulunamadı")
            label = Gtk.Label(label="ITE 8291 sürücüsünü kontrol edin. Kurulumdan sonra uygulamayı yeniden açın.")
            label.set_xalign(0)
            label.set_wrap(True)
            body.append(label)
            page.append(card)
            return page

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="Klavye RGB")
        title.add_css_class("card-title")
        title.set_xalign(0)
        header.append(title)
        subtitle = Gtk.Label(label="4 bölge · RGB 0–50")
        subtitle.set_hexpand(True)
        subtitle.set_xalign(1)
        subtitle.add_css_class("helper-text")
        header.append(subtitle)
        page.append(header)

        zone_card, zone_body = self.make_card("Uygulama alanı", compact=True)
        zone_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        group = None
        for index, label in enumerate(("Tüm klavye", "Bölge 1", "Bölge 2", "Bölge 3", "Bölge 4")):
            button = Gtk.ToggleButton(label=label)
            button.add_css_class("zone-chip")
            if group is None:
                group = button
            else:
                button.set_group(group)
            button.set_active(index == self.zone_selection)
            button.set_tooltip_text(label)
            button.connect("toggled", self.select_zone, index)
            self.zone_buttons.append(button)
            zone_row.append(button)
        zone_body.append(zone_row)
        page.append(zone_card)

        color_card, color_body = self.make_card("Renk ve parlaklık", compact=True)
        color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        color_row.set_valign(Gtk.Align.CENTER)
        self.color_preview = Gtk.DrawingArea()
        self.color_preview.set_content_width(28)
        self.color_preview.set_content_height(26)
        self.color_preview.set_draw_func(self.draw_color_preview)
        color_row.append(self.color_preview)
        self.color_channel_spins = []
        for channel in ("R", "G", "B"):
            group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
            label = Gtk.Label(label=channel)
            label.add_css_class("control-value")
            spin = Gtk.SpinButton.new_with_range(0, 50, 1)
            spin.set_digits(0)
            spin.set_numeric(True)
            spin.set_width_chars(3)
            spin.connect("value-changed", self.on_rgb_changed)
            spin.connect("activate", self.apply_static_color)
            self.color_channel_spins.append(spin)
            group.append(label)
            group.append(spin)
            color_row.append(group)

        color_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        apply_button = Gtk.Button(label="Uygula")
        apply_button.add_css_class("suggested-action")
        apply_button.add_css_class("action-button")
        apply_button.connect("clicked", self.apply_static_color)
        color_actions.append(apply_button)
        off_button = Gtk.Button(label="Kapat")
        off_button.add_css_class("action-button")
        off_button.connect("clicked", self.turn_off_keyboard)
        color_actions.append(off_button)
        color_row.append(color_actions)
        color_body.append(color_row)
        self.set_selected_rgb(self.selected_rgb)

        brightness_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        brightness_label = Gtk.Label(label="Parlaklık")
        brightness_label.add_css_class("control-value")
        brightness_row.append(brightness_label)
        self.brightness_value = Gtk.Label(label="50 / 50")
        self.brightness_value.add_css_class("control-value")
        self.brightness_value.set_size_request(48, -1)
        self.brightness_value.set_xalign(1)
        self.brightness_scale = self.scale(0, 50, 50, 1)
        brightness_row.append(self.brightness_value)
        brightness_row.append(self.brightness_scale)
        color_body.append(brightness_row)
        page.append(color_card)

        effect_card, effect_body = self.make_card("Animasyon", compact=True)
        effect_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.effect_dropdown = Gtk.DropDown.new_from_strings(
            ["Sabit renk", "Gökkuşağı", "Nefes", "Dalga"]
        )
        self.effect_dropdown.set_selected(self.settings["effect"])
        self.effect_dropdown.connect("notify::selected", lambda *_: self.save_settings())
        self.effect_dropdown.set_size_request(140, -1)
        self.effect_dropdown.set_hexpand(True)
        self.effect_dropdown.set_tooltip_text("Klavye efekti")
        effect_controls.append(self.effect_dropdown)
        speed_label = Gtk.Label(label="Hız")
        speed_label.add_css_class("control-value")
        effect_controls.append(speed_label)
        self.speed_scale = self.scale(1, 10, self.settings["speed"], 1)
        self.speed_scale.set_tooltip_text("1 yavaş · 10 hızlı")
        self.speed_scale.connect("value-changed", self.update_speed_value)
        self.speed_scale.connect("value-changed", lambda *_: self.save_settings())
        effect_controls.append(self.speed_scale)
        self.speed_value = Gtk.Label(label="5 / 10")
        self.speed_value.add_css_class("control-value")
        self.speed_value.set_size_request(42, -1)
        self.speed_value.set_xalign(1)
        effect_controls.append(self.speed_value)
        start_button = Gtk.Button(label="Başlat")
        start_button.add_css_class("suggested-action")
        start_button.add_css_class("action-button")
        start_button.connect("clicked", self.start_animation)
        effect_controls.append(start_button)
        stop_button = Gtk.Button(label="Durdur")
        stop_button.add_css_class("action-button")
        stop_button.connect("clicked", self.stop_animation)
        effect_controls.append(stop_button)
        effect_body.append(effect_controls)
        page.append(effect_card)

        self.load_keyboard_state()
        self.brightness_scale.connect("value-changed", self.change_brightness)
        return page

    def build_fan_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        page.set_margin_top(4)
        page.set_margin_bottom(6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="Fanlar")
        title.add_css_class("card-title")
        title.set_xalign(0)
        header.append(title)
        self.fan_mode_label = Gtk.Label(label="EC: okunuyor…")
        self.fan_mode_label.set_xalign(1)
        self.fan_mode_label.set_hexpand(True)
        self.fan_mode_label.add_css_class("control-value")
        header.append(self.fan_mode_label)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh.set_tooltip_text("Durumu yenile")
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self.refresh_fan_status())
        header.append(refresh)
        page.append(header)

        status_card, status_body = self.make_card("Sıcaklık ve fan çıkışı")
        metrics = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        def add_metric(caption: str, value: Gtk.Label):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            cell.set_hexpand(True)
            label = Gtk.Label(label=caption)
            label.set_xalign(0)
            label.add_css_class("helper-text")
            value.add_css_class("compact-value")
            value.set_xalign(0)
            cell.append(label)
            cell.append(value)
            metrics.append(cell)

        self.cpu_temp_value = Gtk.Label(label="— °C")
        self.gpu_temp_value = Gtk.Label(label="— °C")
        self.cpu_fan_value = Gtk.Label(label="—")
        self.gpu_fan_value = Gtk.Label(label="—")
        add_metric("CPU", self.cpu_temp_value)
        add_metric("GPU", self.gpu_temp_value)
        add_metric("CPU fan", self.cpu_fan_value)
        add_metric("GPU fan", self.gpu_fan_value)
        status_body.append(metrics)
        page.append(status_card)

        mode_card, mode_body = self.make_card("Profil")
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        group = None
        for name, value in FAN_MODES.items():
            button = Gtk.ToggleButton(label=name)
            button.add_css_class("mode-chip")
            if group is None:
                group = button
            else:
                button.set_group(group)
            button.connect("clicked", self.set_fan_mode, value)
            self.fan_mode_buttons[value] = button
            mode_row.append(button)
        mode_body.append(mode_row)
        page.append(mode_card)

        manual_card, manual_body = self.make_card("Manuel seviye")
        control_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        self.fan_manual_value = Gtk.Label(label="L3")
        self.fan_manual_value.add_css_class("compact-value")
        self.fan_manual_scale = self.scale(1, 3, self.settings["fan_manual_level"], 1)
        self.fan_manual_scale.set_size_request(150, -1)
        self.fan_manual_scale.connect("value-changed", self.update_manual_fan_value)
        self.fan_manual_scale.connect("value-changed", lambda *_: self.save_settings())
        self.fan_manual_scale.set_tooltip_text("L1 düşük · L3 yüksek")
        self.fan_manual_button = Gtk.Button(label="Uygula")
        self.fan_manual_button.add_css_class("suggested-action")
        self.fan_manual_button.add_css_class("action-button")
        self.fan_manual_button.connect("clicked", self.apply_manual_fan_level)
        control_row.append(Gtk.Label(label="L1"))
        control_row.append(self.fan_manual_scale)
        control_row.append(Gtk.Label(label="L3"))
        control_row.append(self.fan_manual_value)
        control_row.append(self.fan_manual_button)
        manual_body.append(control_row)
        self.restore_check = Gtk.CheckButton(label="Uygulamadan çıkarken Otomatik moda dön")
        self.restore_check.set_active(self.settings["restore_fan_on_exit"])
        self.restore_check.connect("toggled", lambda *_: self.save_settings())
        manual_body.append(self.restore_check)
        page.append(manual_card)

        if not FAN_HELPER.exists():
            self.fan_manual_button.set_sensitive(False)
            self.set_status("Fan arayüzü hazır değil: fan bileşeni kurulu değil.")
        else:
            try:
                self.hw.fan_request({"op": "status"})
            except Exception as exc:
                self.fan_manual_button.set_sensitive(False)
                self.set_status(f"Fan erişimi yok: {exc}")
        GLib.timeout_add_seconds(3, self.refresh_fan_status)
        return page

    @staticmethod
    def scale(low: float, high: float, value: float, step: float) -> Gtk.Scale:
        adjustment = Gtk.Adjustment(value=value, lower=low, upper=high, step_increment=step, page_increment=step * 2)
        scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adjustment)
        scale.set_draw_value(False)
        scale.set_digits(0)
        scale.set_hexpand(True)
        return scale

    def selected_zones(self) -> list[int]:
        return list(range(4)) if self.zone_selection == 0 else [self.zone_selection - 1]

    def select_zone(self, button, index: int):
        if button.get_active():
            self.zone_selection = index
            brightness_index = 0 if index == 0 else index - 1
            brightness = self.settings["zone_brightness"][brightness_index]
            self._syncing_brightness = True
            if self.brightness_scale:
                self.brightness_scale.set_value(brightness)
            self._syncing_brightness = False
            if self.brightness_value:
                self.brightness_value.set_text(f"{brightness} / 50")
            self.save_settings()
            self.set_status("Tüm klavye seçildi." if index == 0 else f"Bölge {index} seçildi.")

    def update_manual_fan_value(self, scale):
        if self.fan_manual_value:
            self.fan_manual_value.set_text(f"L{round(scale.get_value())}")

    def update_speed_value(self, scale):
        if self.speed_value:
            self.speed_value.set_text(f"{round(scale.get_value())} / 10")
        self.save_settings()

    def set_selected_rgb(self, color: tuple[int, int, int]):
        self.selected_rgb = tuple(max(0, min(50, int(value))) for value in color)
        self._syncing_color = True
        for spin, value in zip(self.color_channel_spins, self.selected_rgb):
            spin.set_value(value)
        self._syncing_color = False
        if self.color_preview:
            self.color_preview.queue_draw()

    def on_rgb_changed(self, _spin):
        if self._syncing_color or len(self.color_channel_spins) != 3:
            return
        color = tuple(round(spin.get_value()) for spin in self.color_channel_spins)
        self.set_selected_rgb(color)
        self.settings["selected_rgb"] = list(self.selected_rgb)
        self.save_settings()

    def draw_color_preview(self, _area, cr, width: int, height: int):
        red, green, blue = (value / 50 for value in self.selected_rgb)
        radius = min(8, width / 4, height / 4)
        cr.new_sub_path()
        cr.arc(width - radius, radius, radius, -math.pi / 2, 0)
        cr.arc(width - radius, height - radius, radius, 0, math.pi / 2)
        cr.arc(radius, height - radius, radius, math.pi / 2, math.pi)
        cr.arc(radius, radius, radius, math.pi, math.pi * 1.5)
        cr.close_path()
        cr.set_source_rgb(red, green, blue)
        cr.fill_preserve()
        cr.set_line_width(1)
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.stroke()

    def load_keyboard_state(self):
        if not self.hw.keyboard_ready:
            return
        try:
            if self._settings_loaded:
                brightnesses = self.settings["zone_brightness"]
                self.saved_colors = [tuple(color) for color in self.settings["zone_colors"]]
                for zone in range(4):
                    self.hw.write_led(zone, "multi_intensity", " ".join(map(str, self.saved_colors[zone])))
                    self.hw.write_led(zone, "brightness", str(brightnesses[zone]))
            else:
                brightnesses = [int(self.hw.read_led(zone, "brightness")) for zone in range(4)]
                self.settings["zone_brightness"] = brightnesses
                for zone in range(4):
                    values = [int(value) for value in self.hw.read_led(zone, "multi_intensity").split()]
                    if len(values) == 3:
                        self.saved_colors[zone] = tuple(values)
                        self.settings["zone_colors"][zone] = values
                        if zone == 0:
                            self.set_selected_rgb(tuple(values))
                            self.settings["selected_rgb"] = values
            brightness = brightnesses[0 if self.zone_selection == 0 else self.zone_selection - 1]
            self.brightness_scale.set_value(brightness)
            self.brightness_value.set_text(f"{brightness} / 50")
            self.set_selected_rgb(tuple(self.settings["selected_rgb"]))
        except Exception as exc:
            self.set_status(f"Klavye durumu okunamadı: {exc}")

    def apply_static_color(self, *_):
        self.stop_animation()
        try:
            for zone in self.selected_zones():
                self.write_zone_color(zone, self.selected_rgb)
            self.set_status("Klavye rengi uygulandı.")
        except Exception as exc:
            self.set_status(f"Klavye rengi uygulanamadı: {exc}")

    def write_zone_color(self, zone: int, color: tuple[int, int, int]):
        color = tuple(max(0, min(50, int(c))) for c in color)
        self.hw.write_led(zone, "multi_intensity", " ".join(map(str, color)))
        self.saved_colors[zone] = color
        self.settings["zone_colors"][zone] = list(color)
        self.save_settings()

    def turn_off_keyboard(self, *_):
        self.stop_animation()
        try:
            for zone in self.selected_zones():
                self.write_zone_color(zone, (0, 0, 0))
            self.set_status("Seçili klavye bölgesi kapatıldı.")
        except Exception as exc:
            self.set_status(f"Klavye kapatılamadı: {exc}")

    def change_brightness(self, scale):
        value_number = round(scale.get_value())
        if self.brightness_value:
            self.brightness_value.set_text(f"{value_number} / 50")
        if self._syncing_brightness:
            return
        for zone in self.selected_zones():
            self.settings["zone_brightness"][zone] = value_number
        self.save_settings()
        if not self.hw.keyboard_ready:
            return
        value = str(value_number)
        try:
            for zone in self.selected_zones():
                self.hw.write_led(zone, "brightness", value)
        except Exception as exc:
            self.set_status(f"Parlaklık değiştirilemedi: {exc}")

    def start_animation(self, *_):
        if not self.hw.keyboard_ready:
            self.set_status("Klavye sürücüsü hazır değil.")
            return
        self.stop_animation(persist=False)
        self.settings["animation_running"] = True
        self.save_settings()
        self.animation_start = time.monotonic()
        self.animation_last = 0.0
        self.animation_id = GLib.timeout_add(60, self.animation_tick)
        self.set_status("Animasyon çalışıyor.")

    def stop_animation(self, *_args, persist=True):
        if self.animation_id is not None:
            GLib.source_remove(self.animation_id)
            self.animation_id = None
        if persist:
            self.settings["animation_running"] = False
            self.save_settings()

    def animation_tick(self) -> bool:
        now = time.monotonic()
        speed = self.speed_scale.get_value()
        interval = 1.05 - (speed - 1) * 0.095
        if now - self.animation_last < interval:
            return True
        self.animation_last = now
        effect = self.effect_dropdown.get_selected()
        zones = self.selected_zones()
        base = self.selected_rgb
        phase = (now - self.animation_start) * (0.15 + speed * 0.09)
        try:
            for index, zone in enumerate(zones):
                if effect == 0:  # Static color
                    color = base
                elif effect == 1:  # Rainbow
                    color = tuple(round(c * 50) for c in colorsys.hsv_to_rgb((phase / 6 + index / max(1, len(zones))) % 1.0, 1.0, 1.0))
                elif effect == 2:  # Breathing
                    factor = 0.08 + 0.92 * (0.5 + 0.5 * math.sin(phase * 2))
                    color = tuple(round(c * factor) for c in base)
                else:  # Four-zone wave
                    offset = (index - int(phase * 2)) % max(1, len(zones))
                    factor = 1.0 if offset == 0 else 0.14
                    color = tuple(round(c * factor) for c in base)
                self.hw.write_led(zone, "multi_intensity", " ".join(map(str, color)))
                self.saved_colors[zone] = color
        except Exception as exc:
            self.stop_animation()
            self.set_status(f"Animasyon durdu: {exc}")
            return False
        return True

    def set_fan_mode(self, _button, mode: int):
        try:
            self.hw.set_fan_mode(mode)
            self.settings["fan_mode"] = mode
            self.save_settings()
            self.refresh_fan_status()
            self.set_status(f"Fan modu: {MODE_NAMES[mode]}.")
        except Exception as exc:
            self.set_status(f"Fan modu değiştirilemedi: {exc}")

    def apply_manual_fan_level(self, *_):
        try:
            level = round(self.fan_manual_scale.get_value())
            self.hw.set_fixed_fan_level(level)
            self.settings["fan_mode"] = 0x80 + level
            self.save_settings()
            self.refresh_fan_status()
            self.set_status(f"Sabit fan seviyesi L{level} uygulandı.")
        except Exception as exc:
            try:
                self.hw.set_fan_mode(0x00)
            except Exception:
                pass
            self.refresh_fan_status()
            self.set_status(f"Sabit fan seviyesi uygulanamadı: {exc}")

    def restore_saved_fan_mode(self):
        if not self._settings_loaded:
            return
        mode = self.settings["fan_mode"]
        try:
            if mode in FAN_MODES.values():
                self.hw.set_fan_mode(mode)
            elif mode in (0x81, 0x82, 0x83):
                self.hw.set_fixed_fan_level(mode - 0x80)
        except Exception as exc:
            self.set_status(f"Kayıtlı fan ayarı uygulanamadı: {exc}")

    def refresh_fan_status(self, *_):
        if not self.fan_mode_label:
            return GLib.SOURCE_CONTINUE
        try:
            status = self.hw.fan_request({"op": "status"})
            mode = status["mode"]
            mode_name = MODE_NAMES.get(mode, f"Bilinmeyen (0x{mode:02x})")
            cpu = status.get("cpu_temp")
            gpu = self.hw.get_gpu_temperature()
            cpu_text = f"{cpu} °C" if cpu is not None else "— °C"
            gpu_text = f"{gpu} °C" if gpu is not None else "— °C"
            self.fan_mode_label.set_text(f"EC: {mode_name}")
            self.cpu_temp_value.set_text(cpu_text)
            self.gpu_temp_value.set_text(gpu_text)
            cpu_fan = status.get("cpu_fan_raw")
            gpu_fan = status.get("gpu_fan_raw")
            self.cpu_fan_value.set_text(f"%{round(cpu_fan / 2)}" if cpu_fan is not None and 0 <= cpu_fan <= 200 else "—")
            self.gpu_fan_value.set_text(f"%{round(gpu_fan / 2)}" if gpu_fan is not None and 0 <= gpu_fan <= 200 else "—")
            for fan_mode, button in self.fan_mode_buttons.items():
                button.set_active(mode == fan_mode)
            if mode in (0x00, 0x10, 0x40, 0x81, 0x82, 0x83):
                self.settings["fan_mode"] = mode
                self.save_settings()
            if self.device_badge:
                self.device_badge.set_text("● Donanım hazır")
        except Exception as exc:
            self.fan_mode_label.set_text(f"Fan arayüzü hazır değil: {exc}")
            if self.device_badge:
                self.device_badge.set_text("● Fan denetleyicisi erişilemiyor")
        return GLib.SOURCE_CONTINUE

    def set_status(self, message: str):
        if self.status:
            self.status.set_text(message)
            if self.status_icon:
                is_error = any(token in message.lower() for token in ("olmadı", "hata", "erişilemiyor", "başarısız"))
                self.status_icon.set_from_icon_name(
                    "dialog-error-symbolic" if is_error else "emblem-ok-symbolic"
                )

    def on_close(self, *_):
        if self.tray and self.tray.registered:
            self.save_settings()
            self.window.set_visible(False)
            return True
        self.set_status("Sistem tepsisi hazır değil; uygulama kapanmadan açık kalıyor.")
        return True

    def on_shutdown(self, *_):
        self._closing = True
        self.stop_animation(persist=False)
        self.save_settings()
        if self.restore_check and self.restore_check.get_active():
            try:
                if self.hw.get_fan_mode() in (0x10, 0x40, 0xA0, 0x81, 0x82, 0x83):
                    self.hw.set_fan_mode(0x00)
            except Exception:
                pass


def main():
    app = AbraControl()
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
