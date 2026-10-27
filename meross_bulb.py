#!/usr/bin/env python3
"""Meross RGB bulb controller for Linux Mint (GTK3). Local LAN control, no cloud at runtime.

Run:      python3 meross_bulb.py
Get key:  python3 meross_bulb.py --get-key you@example.com [--api https://iotx-eu.meross.com]
          (one-off cloud login to fetch your account "key"; needs `pip install meross_iot` in a venv)
"""
import colorsys
import concurrent.futures as cf
import hashlib
import json
import math
import os
import socket
import sys
import threading
import time
import uuid
from datetime import datetime

import requests

CLI_CMDS = {"status", "toggle", "on", "off", "bright", "color", "white", "preset", "presets", "rofi"}
CLI_MODE = len(sys.argv) > 1 and (sys.argv[1] in CLI_CMDS or sys.argv[1] in ("-b", "--bulb", "--all", "-h", "--help"))
if CLI_MODE:  # command-line use (polybar / rofi / hotkeys) must not load or initialise GTK
    from types import SimpleNamespace
    Gtk = SimpleNamespace(Window=object)
else:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib
    GLib.set_prgname("meross-bulb")  # window class becomes "Meross-bulb" so the launcher/taskbar matches
    from gi.repository import Gtk, Gdk  # noqa: E402
    try:
        import cairo  # needed for the colour wheel (apt: python3-gi-cairo)
    except ImportError:
        cairo = None

CONFIG = os.path.expanduser("~/.config/meross-bulb/config.json")


# ---------------------------------------------------------------- config
def load_cfg():
    try:
        with open(CONFIG) as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    c.setdefault("key", "")
    c.setdefault("devices", [])  # [{"name":..., "ip":...}]
    c.setdefault("presets", {})  # {name: state}
    return c


def save_cfg(c):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(c, f, indent=2)
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- Meross local protocol
def packet(ns, method, payload, key):
    mid = uuid.uuid4().hex
    ts = int(time.time())
    sign = hashlib.md5(f"{mid}{key}{ts}".encode()).hexdigest()
    return {
        "header": {
            "messageId": mid, "namespace": ns, "method": method, "payloadVersion": 1,
            "from": "http://localhost/config", "timestamp": ts, "timestampMs": 0, "sign": sign,
        },
        "payload": payload,
    }


def post(ip, key, ns, method, payload, timeout=3):
    r = requests.post(f"http://{ip}/config", json=packet(ns, method, payload, key), timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if d.get("header", {}).get("method") == "ERROR":
        err = d.get("payload", {}).get("error", {})
        hint = " (wrong/missing key?)" if err.get("code") == 5001 else ""
        raise RuntimeError(f"bulb error {err}{hint}")
    return d


def hs_to_int(h, s):
    r, g, b = colorsys.hsv_to_rgb(h / 360, s / 100, 1.0)  # brightness is sent separately as luminance
    return (round(r * 255) << 16) | (round(g * 255) << 8) | round(b * 255)


def int_to_hs(v):
    r, g, b = (v >> 16) & 255, (v >> 8) & 255, v & 255
    h, s, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return round(h * 360), round(s * 100)


INVERT_TEMP = False  # set True if the White slider's warm/cool direction is backwards


def apply_state(ip, key, st):
    lum = max(1, st["lum"])
    if not st["on"]:
        try:
            post(ip, key, "Appliance.Control.ToggleX", "SET", {"togglex": {"channel": 0, "onoff": 0}})
        except Exception:
            post(ip, key, "Appliance.Control.Light", "SET",
                 {"light": {"channel": 0, "onoff": 0, "luminance": lum, "capacity": 4}})
        return
    try:  # power on via ToggleX (what the Meross app uses), then set the look
        post(ip, key, "Appliance.Control.ToggleX", "SET", {"togglex": {"channel": 0, "onoff": 1}})
    except Exception:
        pass
    light = {"channel": 0, "onoff": 1, "luminance": lum}
    if st["mode"] == "rgb":
        light["rgb"] = hs_to_int(st["hue"], st["sat"])
        light["capacity"] = 5  # rgb + luminance
    else:
        light["temperature"] = 101 - st["temp"] if INVERT_TEMP else st["temp"]
        light["capacity"] = 6  # temperature + luminance
    post(ip, key, "Appliance.Control.Light", "SET", {"light": light})


def read_state(ip, key, timeout=3):
    dig = post(ip, key, "Appliance.System.All", "GET", {}, timeout)["payload"]["all"]["digest"]
    l = dict(dig.get("light", {}))
    tx = dig.get("togglex")  # the authoritative on/off state on most bulbs
    for t in ([tx] if isinstance(tx, dict) else tx or []):
        if t.get("channel", 0) == 0 and "onoff" in t:
            l["onoff"] = t["onoff"]
    return l


DEFAULT_STATE = {"on": True, "mode": "rgb", "hue": 0, "sat": 100, "lum": 100, "temp": 50}


def light_to_state(l, base):
    st = dict(base)
    st["on"] = bool(l.get("onoff", 1))
    st["lum"] = int(l.get("luminance", st["lum"]))
    cap = l.get("capacity", 5)
    if cap & 2 and not cap & 1:
        t = int(l.get("temperature", st["temp"]))
        st["mode"], st["temp"] = "temp", (101 - t if INVERT_TEMP else t)
    else:
        st["mode"] = "rgb"
        if "rgb" in l:
            st["hue"], st["sat"] = int_to_hs(int(l["rgb"]))
    return st


# ---------------------------------------------------------------- diagnostics (read-only GETs)
def fetch_info(ip, key, want_ability=True):
    t0 = time.time()
    info = {"all": post(ip, key, "Appliance.System.All", "GET", {})["payload"]}
    info["ms"] = round((time.time() - t0) * 1000)
    calls = [("debug", "Appliance.System.Debug")]
    if want_ability:  # supported-feature list never changes, so fetch it once per bulb
        calls.append(("ability", "Appliance.System.Ability"))
    for name, ns in calls:
        try:
            info[name] = post(ip, key, ns, "GET", {})["payload"]
        except Exception as e:
            info[name] = {"error": str(e)}
    return info


def fmt_info(ip, info, ability, facts):
    def g(d, k):
        v = d.get(k) if isinstance(d, dict) else None
        return "-" if v in (None, "") else v

    def kv(k, v):
        return f"  {k:<18}{v}"

    a = info["all"].get("all", {})
    sysd = a.get("system", {})
    hw, fw, tm, onl = (sysd.get(k, {}) for k in ("hardware", "firmware", "time", "online"))
    dbg = info.get("debug", {}).get("debug", {})
    ds, net, cl = (dbg.get(k, {}) for k in ("system", "network", "cloud"))
    light = a.get("digest", {}).get("light", {})
    ts = tm.get("timestamp")
    devtime = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, (int, float)) else "-"

    L = [f"Updated {datetime.now():%H:%M:%S}  -  {ip}  -  round trip {info['ms']} ms", "", "DEVICE",
         kv("Model", f"{g(hw, 'type')}  (hw {g(hw, 'version')}, chip {g(hw, 'chipType')})"),
         kv("Firmware", f"{g(fw, 'version')}  built {g(fw, 'compileTime')}"),
         kv("UUID", g(hw, "uuid")),
         kv("MAC", g(hw, "macAddress") if g(hw, "macAddress") != "-" else g(fw, "wifiMac")),
         kv("Device clock", f"{devtime}  tz {g(tm, 'timezone')}"),
         kv("Uptime", g(ds, "sysUpTime")),
         kv("Online (cloud)", g(onl, "status")),
         "", "NETWORK",
         kv("Inner IP", g(net, "innerIp") if g(net, "innerIp") != "-" else g(fw, "innerIp")),
         kv("Wi-Fi signal", g(net, "signal")),
         kv("Link", f"status {g(net, 'linkStatus')}  type {g(net, 'linkType')}"),
         kv("Gateway MAC", g(net, "gatewayMac")),
         "", "CLOUD LINK (what the bulb itself is doing)",
         kv("Active server", g(cl, "activeServer")),
         kv("Main server", f"{g(cl, 'mainServer')}:{g(cl, 'mainPort')}"),
         kv("Connected for", g(cl, "sysConnectTime")),
         kv("Online time", g(cl, "sysOnlineTime")),
         kv("Disconnects", g(cl, "sysDisconnectCount")),
         "", "LIGHT STATE"]
    for k, v in light.items():
        extra = f"  (#{v:06x})" if k == "rgb" and isinstance(v, int) else ""
        L.append(kv(k, f"{v}{extra}"))
    L.append(kv("togglex (power)", json.dumps(a.get("digest", {}).get("togglex", "-"))))
    L += ["", "THIS APP",
          kv("Config file", CONFIG),
          kv("Account key", "set" if facts["key"] else "not set (empty-key signing)"),
          kv("Bulbs / presets", f"{facts['devices']} / {facts['presets']}")]
    ab = ability.get("ability") if isinstance(ability, dict) else None
    L += ["", f"SUPPORTED NAMESPACES ({len(ab) if ab else 0})"]
    if ab:
        L += [f"  {n}" for n in sorted(ab)]
    else:
        L.append(f"  unavailable: {ability.get('error', '?') if isinstance(ability, dict) else '?'}")
    L += ["", "RAW Appliance.System.All", json.dumps(info["all"], indent=1),
          "", "RAW Appliance.System.Debug", json.dumps(info.get("debug", {}), indent=1)]
    return "\n".join(L)


# ---------------------------------------------------------------- LAN scan
def local_prefix():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0].rsplit(".", 1)[0]
    finally:
        s.close()


def probe(ip):
    try:
        r = requests.post(f"http://{ip}/config",
                          json=packet("Appliance.System.All", "GET", {}, ""), timeout=0.8)
        return ip if "namespace" in r.json().get("header", {}) else None
    except Exception:
        return None


def scan():
    pre = local_prefix()
    with cf.ThreadPoolExecutor(64) as ex:
        return [ip for ip in ex.map(probe, [f"{pre}.{i}" for i in range(1, 255)]) if ip]


# ---------------------------------------------------------------- one-off cloud key fetch
def cli_get_key(email, api):
    import asyncio
    import getpass
    from meross_iot.http_api import MerossHttpClient

    async def go():
        c = await MerossHttpClient.async_from_user_password(
            api_base_url=api, email=email, password=getpass.getpass("Meross password: "))
        key = c.cloud_credentials.key
        devs = await c.async_list_devices()
        await c.async_logout()
        return key, devs

    key, devs = asyncio.run(go())
    cfg = load_cfg()
    cfg["key"] = key
    save_cfg(cfg)
    print("Key saved to", CONFIG)
    for d in devs:
        print(" -", getattr(d, "dev_name", "?"), getattr(d, "device_type", "?"), getattr(d, "uuid", "?"))


# ---------------------------------------------------------------- GUI
CSS = b"""
scale.hue trough { background-image: linear-gradient(to right,#f00,#ff0,#0f0,#0ff,#00f,#f0f,#f00); min-height: 10px; }
scale.hue trough highlight { background: none; }
scale.temp trough { background-image: linear-gradient(to right,#ffa94d,#fff1e0,#d6e6ff); min-height: 10px; }
scale.temp trough highlight { background: none; }
"""
SWATCHES = [(0, 100), (30, 100), (55, 100), (120, 100), (180, 100), (240, 100), (280, 100), (320, 100), (0, 0)]


class App(Gtk.Window):
    WHEEL = 220  # colour wheel size in px

    def __init__(self):
        super().__init__(title="Meross Bulbs")
        self.set_border_width(12)
        self.set_default_size(480, 0)
        self.cfg = load_cfg()
        self.loading = False
        self.timer = None
        self.lock = threading.Lock()
        self.pending = None
        self.busy = False
        self.st = {"on": True, "mode": "rgb", "hue": 0, "sat": 100, "lum": 100, "temp": 50}

        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.nb = Gtk.Notebook()
        self.add(self.nb)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_border_width(10)
        self.nb.append_page(root, Gtk.Label(label="Control"))

        # devices
        row = Gtk.Box(spacing=6)
        self.combo = Gtk.ComboBoxText()
        self.combo.set_hexpand(True)
        self.combo.connect("changed", lambda *_: (self.pull(), self.info_on_dev()))
        row.pack_start(self.combo, True, True, 0)
        for label, cb in (("Add", self.add_dev), ("Scan", self.do_scan),
                          ("Remove", self.rm_dev), ("Key", self.set_key)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            row.pack_start(b, False, False, 0)
        root.pack_start(row, False, False, 0)

        # power
        row = Gtk.Box(spacing=8)
        self.sw = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        self.sw.connect("notify::active", self.changed)
        row.pack_start(Gtk.Label(label="Power"), False, False, 0)
        row.pack_start(self.sw, False, False, 0)
        self.all_chk = Gtk.CheckButton(label="Control all bulbs")
        row.pack_end(self.all_chk, False, False, 0)
        root.pack_start(row, False, False, 0)

        # mode
        row = Gtk.Box(spacing=12)
        self.rb_c = Gtk.RadioButton.new_with_label(None, "Color")
        self.rb_w = Gtk.RadioButton.new_with_label_from_widget(self.rb_c, "White")
        self.rb_c.connect("toggled", self.changed)
        row.pack_start(self.rb_c, False, False, 0)
        row.pack_start(self.rb_w, False, False, 0)
        root.pack_start(row, False, False, 0)

        # colour wheel (hue = angle, saturation = distance from the centre)
        self.wheel = None
        self.wheel_surf = None
        self.wheel_drag = False
        if cairo:
            self.wheel = Gtk.DrawingArea()
            self.wheel.set_size_request(self.WHEEL, self.WHEEL)
            self.wheel.set_halign(Gtk.Align.CENTER)
            self.wheel.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                                  | Gdk.EventMask.BUTTON1_MOTION_MASK)
            self.wheel.connect("draw", self.draw_wheel)
            for sig in ("button-press-event", "motion-notify-event", "button-release-event"):
                self.wheel.connect(sig, self.wheel_event)
            root.pack_start(self.wheel, False, False, 0)

        # sliders
        g = Gtk.Grid(row_spacing=6, column_spacing=10)
        self.s_lum = self.scale(0, 100, 100)
        self.s_hue = self.scale(0, 360, 0)
        self.s_hue.get_style_context().add_class("hue")
        self.s_sat = self.scale(0, 100, 100)
        self.s_tmp = self.scale(1, 100, 50)
        self.s_tmp.get_style_context().add_class("temp")
        for i, (name, w) in enumerate((("Brightness %", self.s_lum), ("Hue", self.s_hue),
                                       ("Saturation %", self.s_sat), ("White (warm\u2192cool)", self.s_tmp))):
            g.attach(Gtk.Label(label=name, xalign=0), 0, i, 1, 1)
            g.attach(w, 1, i, 1, 1)
            g.attach(w.vlabel, 2, i, 1, 1)
        root.pack_start(g, False, False, 0)

        # swatches + picker + preview
        row = Gtk.Box(spacing=4)
        for h, s in SWATCHES:
            r, gg, b = colorsys.hsv_to_rgb(h / 360, s / 100, 1)
            hexc = "#%02x%02x%02x" % (round(r * 255), round(gg * 255), round(b * 255))
            btn = Gtk.Button()
            lab = Gtk.Label()
            lab.set_markup(f'<span foreground="{hexc}" size="x-large">●</span>')
            btn.add(lab)
            btn.connect("clicked", lambda _b, h=h, s=s: self.set_hs(h, s))
            row.pack_start(btn, False, False, 0)
        self.picker = Gtk.ColorButton()
        self.picker.connect("color-set", self.on_pick)
        row.pack_start(self.picker, False, False, 6)
        self.preview = Gtk.DrawingArea()
        self.preview.set_size_request(60, 26)
        self.preview.connect("draw", self.draw)
        row.pack_end(self.preview, True, True, 0)
        root.pack_start(row, False, False, 0)

        # presets
        root.pack_start(Gtk.Separator(), False, False, 2)
        row = Gtk.Box(spacing=6)
        self.pcombo = Gtk.ComboBoxText()
        self.pcombo.set_hexpand(True)
        row.pack_start(self.pcombo, True, True, 0)
        for label, cb in (("Apply", self.p_apply), ("Delete", self.p_delete)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            row.pack_start(b, False, False, 0)
        root.pack_start(row, False, False, 0)
        row = Gtk.Box(spacing=6)
        self.pname = Gtk.Entry(placeholder_text="Preset name")
        self.pname.set_hexpand(True)
        b = Gtk.Button(label="Save current as preset")
        b.connect("clicked", self.p_save)
        row.pack_start(self.pname, True, True, 0)
        row.pack_start(b, False, False, 0)
        root.pack_start(row, False, False, 0)

        self.status = Gtk.Label(label="", xalign=0)
        root.pack_start(self.status, False, False, 0)

        self.ability_cache = {}
        self.info_busy = False
        self.nb.append_page(self.build_info(), Gtk.Label(label="Info"))
        self.nb.connect("switch-page", lambda nb, page, n: GLib.idle_add(self.info_refresh) if n == 1 else None)
        GLib.timeout_add_seconds(10, self.info_tick)

        self.refresh_devs()
        self.refresh_presets()
        self.sensitivity()
        self.connect("destroy", Gtk.main_quit)
        self.show_all()

    # ---- helpers
    def scale(self, lo, hi, val):
        s = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, 1)
        s.set_value(val)
        s.set_digits(0)
        s.set_hexpand(True)
        s.set_draw_value(False)
        s.vlabel = Gtk.Label(label=str(val), width_chars=4, xalign=1)
        s.connect("value-changed", lambda w: w.vlabel.set_text(str(int(w.get_value()))))
        s.connect("value-changed", self.changed)
        return s

    def ask(self, title, labels, defaults=None):
        d = Gtk.Dialog(title=title, transient_for=self, modal=True)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        box = d.get_content_area()
        box.set_spacing(6)
        box.set_border_width(10)
        es = []
        for i, l in enumerate(labels):
            box.add(Gtk.Label(label=l, xalign=0))
            e = Gtk.Entry()
            e.set_activates_default(True)
            e.set_text(defaults[i] if defaults else "")
            box.add(e)
            es.append(e)
        d.set_default_response(Gtk.ResponseType.OK)
        d.show_all()
        ok = d.run() == Gtk.ResponseType.OK
        vals = [e.get_text().strip() for e in es]
        d.destroy()
        return vals if ok else None

    def say(self, msg):
        self.status.set_text(msg)

    def sensitivity(self):
        rgb = self.rb_c.get_active()
        self.s_hue.set_sensitive(rgb)
        self.s_sat.set_sensitive(rgb)
        self.picker.set_sensitive(rgb)
        self.s_tmp.set_sensitive(not rgb)
        wheel = getattr(self, "wheel", None)
        if wheel:
            wheel.set_sensitive(rgb)
            wheel.queue_draw()

    def draw(self, _w, cr):
        st = self.st
        if st["mode"] == "rgb":
            r, g, b = colorsys.hsv_to_rgb(st["hue"] / 360, st["sat"] / 100, 1)
        else:
            r, g, b = 1.0, 0.9, 0.75
        k = 0.15 + 0.85 * st["lum"] / 100
        cr.set_source_rgb(r * k, g * k, b * k)
        cr.paint()
        return False

    # ---- colour wheel
    def wheel_surface(self):
        S = self.WHEEL
        R = S / 2 - 3
        buf = bytearray(S * S * 4)
        for y in range(S):
            for x in range(S):
                dx, dy = x + 0.5 - S / 2, y + 0.5 - S / 2
                r = math.hypot(dx, dy)
                a = max(0.0, min(1.0, R - r + 0.5))  # soft edge
                if a <= 0:
                    continue
                h = (math.atan2(dy, dx) % (2 * math.pi)) / (2 * math.pi)
                rr, gg, bb = colorsys.hsv_to_rgb(h, min(1.0, r / R), 1.0)
                i = (y * S + x) * 4  # premultiplied ARGB32, little-endian = B, G, R, A
                buf[i:i + 4] = bytes((round(bb * 255 * a), round(gg * 255 * a), round(rr * 255 * a), round(a * 255)))
        self._wheel_buf = buf
        return cairo.ImageSurface.create_for_data(buf, cairo.FORMAT_ARGB32, S, S, S * 4)

    def draw_wheel(self, w, cr):
        if self.wheel_surf is None:
            self.wheel_surf = self.wheel_surface()
        S = self.WHEEL
        live = w.is_sensitive()
        cr.set_source_surface(self.wheel_surf, 0, 0)
        if live:
            cr.paint()
        else:
            cr.paint_with_alpha(0.3)  # greyed out in White mode
            return False
        R = S / 2 - 3
        ang = math.radians(self.st["hue"])
        rad = self.st["sat"] / 100 * R
        x, y = S / 2 + rad * math.cos(ang), S / 2 + rad * math.sin(ang)
        r, g, b = colorsys.hsv_to_rgb(self.st["hue"] / 360, self.st["sat"] / 100, 1.0)
        cr.arc(x, y, 8, 0, 2 * math.pi)
        cr.set_source_rgb(r, g, b)
        cr.fill()
        cr.arc(x, y, 8, 0, 2 * math.pi)
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_width(2.5)
        cr.stroke()
        cr.arc(x, y, 9.5, 0, 2 * math.pi)
        cr.set_source_rgb(0, 0, 0)
        cr.set_line_width(1)
        cr.stroke()
        return False

    def wheel_event(self, w, ev):
        t = ev.type
        if t == Gdk.EventType.BUTTON_RELEASE:
            self.wheel_drag = False
            return True
        S = self.WHEEL
        R = S / 2 - 3
        dx, dy = ev.x - S / 2, ev.y - S / 2
        r = math.hypot(dx, dy)
        if t == Gdk.EventType.BUTTON_PRESS:
            self.wheel_drag = r <= R + 6  # only start dragging from on/near the wheel
        if self.wheel_drag:
            self.set_hs(round(math.degrees(math.atan2(dy, dx)) % 360), round(min(1.0, r / R) * 100))
        return True

    # ---- state <-> widgets
    def changed(self, *_):
        if self.loading:
            return
        self.st.update(
            on=self.sw.get_active(),
            mode="rgb" if self.rb_c.get_active() else "temp",
            hue=int(self.s_hue.get_value()), sat=int(self.s_sat.get_value()),
            lum=int(self.s_lum.get_value()), temp=int(self.s_tmp.get_value()))
        self.sensitivity()
        self.preview.queue_draw()
        self.schedule()

    def load_state(self, st):
        self.loading = True
        self.st.update(st)
        self.sw.set_active(bool(st["on"]))
        (self.rb_c if st["mode"] == "rgb" else self.rb_w).set_active(True)
        self.s_hue.set_value(st["hue"])
        self.s_sat.set_value(st["sat"])
        self.s_lum.set_value(st["lum"])
        self.s_tmp.set_value(st["temp"])
        self.loading = False
        self.sensitivity()
        self.preview.queue_draw()

    def set_hs(self, h, s):
        self.rb_c.set_active(True)
        self.s_hue.set_value(h)
        self.s_sat.set_value(s)

    def on_pick(self, btn):
        c = btn.get_rgba()
        h, s, _ = colorsys.rgb_to_hsv(c.red, c.green, c.blue)
        self.set_hs(round(h * 360), round(s * 100))

    # ---- sending
    def targets(self):
        devs = self.cfg["devices"]
        if self.all_chk.get_active():
            return [d["ip"] for d in devs]
        i = self.combo.get_active()
        return [devs[i]["ip"]] if 0 <= i < len(devs) else []

    def schedule(self):
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = GLib.timeout_add(60, self.fire)

    def fire(self):
        self.timer = None
        ips, st, key = self.targets(), dict(self.st), self.cfg["key"]
        if not ips:
            self.say("Add or select a bulb first.")
            return False

        with self.lock:
            self.pending = (ips, st, key)
            if self.busy:
                return False
            self.busy = True
        threading.Thread(target=self.sender, daemon=True).start()
        return False

    def sender(self):
        # One request at a time; if the slider moved meanwhile, only the newest value is sent.
        while True:
            with self.lock:
                job, self.pending = self.pending, None
                if job is None:
                    self.busy = False
                    return
            ips, st, key = job
            errs = []
            for ip in ips:
                try:
                    apply_state(ip, key, st)
                    cache_put(ip, st)
                except Exception as e:
                    errs.append(f"{ip}: {e}")
            GLib.idle_add(self.say, "; ".join(errs) if errs else "OK")

    def pull(self):
        i = self.combo.get_active()
        if not 0 <= i < len(self.cfg["devices"]):
            return
        ip, key = self.cfg["devices"][i]["ip"], self.cfg["key"]

        def work():
            try:
                l = read_state(ip, key)
            except Exception as e:
                GLib.idle_add(self.say, f"{ip}: {e}")
                return
            st = light_to_state(l, self.st)
            cache_put(ip, st)
            GLib.idle_add(self.load_state, st)
            GLib.idle_add(self.say, f"Connected to {ip}")

        threading.Thread(target=work, daemon=True).start()

    # ---- devices
    def refresh_devs(self, select=0):
        self.combo.remove_all()
        for d in self.cfg["devices"]:
            self.combo.append_text(f'{d["name"]} ({d["ip"]})')
        if self.cfg["devices"]:
            self.combo.set_active(min(select, len(self.cfg["devices"]) - 1))

    def add_dev(self, *_):
        v = self.ask("Add bulb", ["Name", "IP address"])
        if v and v[1]:
            self.cfg["devices"].append({"name": v[0] or v[1], "ip": v[1]})
            save_cfg(self.cfg)
            self.refresh_devs(len(self.cfg["devices"]) - 1)

    def rm_dev(self, *_):
        i = self.combo.get_active()
        if 0 <= i < len(self.cfg["devices"]):
            del self.cfg["devices"][i]
            save_cfg(self.cfg)
            self.refresh_devs()

    def set_key(self, *_):
        v = self.ask("Meross account key", ["Key (leave empty if bulb was never bound to the cloud)"],
                     [self.cfg["key"]])
        if v is not None:
            self.cfg["key"] = v[0]
            save_cfg(self.cfg)

    def do_scan(self, *_):
        self.say("Scanning local /24 for Meross devices...")

        def work():
            try:
                ips = scan()
            except Exception as e:
                GLib.idle_add(self.say, f"Scan failed: {e}")
                return
            GLib.idle_add(self.scanned, ips)

        threading.Thread(target=work, daemon=True).start()

    def scanned(self, ips):
        known = {d["ip"] for d in self.cfg["devices"]}
        new = [ip for ip in ips if ip not in known]
        for ip in new:
            self.cfg["devices"].append({"name": f"Meross {ip.rsplit('.', 1)[1]}", "ip": ip})
        save_cfg(self.cfg)
        self.refresh_devs(self.combo.get_active() if self.combo.get_active() >= 0 else 0)
        self.say(f"Found {len(ips)} Meross device(s), added {len(new)}. (Includes non-bulbs like plugs.)")

    # ---- info tab
    def build_info(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(10)
        row = Gtk.Box(spacing=8)
        b = Gtk.Button(label="Refresh")
        b.connect("clicked", self.info_refresh)
        c = Gtk.Button(label="Copy")
        c.connect("clicked", self.info_copy)
        self.auto_chk = Gtk.CheckButton(label="Auto-refresh every 10 s (only while this tab is open)")
        row.pack_start(b, False, False, 0)
        row.pack_start(c, False, False, 0)
        row.pack_start(self.auto_chk, False, False, 6)
        box.pack_start(row, False, False, 0)
        self.tv = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.tv.set_wrap_mode(Gtk.WrapMode.NONE)
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(420)
        sw.add(self.tv)
        box.pack_start(sw, True, True, 0)
        self.info_set("Select a bulb, then press Refresh.")
        return box

    def info_set(self, text):
        self.tv.get_buffer().set_text(text)

    def info_copy(self, *_):
        buf = self.tv.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)

    def info_on_dev(self):
        if self.nb.get_current_page() == 1:
            self.info_refresh()

    def info_tick(self):
        if self.auto_chk.get_active() and self.nb.get_current_page() == 1:
            self.info_refresh()
        return True

    def info_refresh(self, *_):
        i = self.combo.get_active()
        if not 0 <= i < len(self.cfg["devices"]):
            self.info_set("Add or select a bulb first.")
            return False
        if self.info_busy:
            return False
        self.info_busy = True
        ip, key = self.cfg["devices"][i]["ip"], self.cfg["key"]
        facts = {"key": bool(key), "devices": len(self.cfg["devices"]), "presets": len(self.cfg["presets"])}
        want = ip not in self.ability_cache

        def work():
            try:
                info = fetch_info(ip, key, want)
                if want and "error" not in info["ability"]:
                    self.ability_cache[ip] = info["ability"]
                text = fmt_info(ip, info, self.ability_cache.get(ip, info.get("ability", {})), facts)
            except Exception as e:
                text = f"{ip}: {e}"
            GLib.idle_add(self.info_done, text)

        threading.Thread(target=work, daemon=True).start()
        return False

    def info_done(self, text):
        self.info_busy = False
        self.info_set(text)

    # ---- presets
    def refresh_presets(self, select=None):
        self.pcombo.remove_all()
        names = sorted(self.cfg["presets"])
        for n in names:
            self.pcombo.append_text(n)
        if names:
            self.pcombo.set_active(names.index(select) if select in names else 0)

    def p_save(self, *_):
        n = self.pname.get_text().strip()
        if not n:
            self.say("Type a preset name first.")
            return
        self.cfg["presets"][n] = dict(self.st)
        save_cfg(self.cfg)
        self.refresh_presets(n)
        self.say(f"Saved preset '{n}'")

    def p_apply(self, *_):
        n = self.pcombo.get_active_text()
        if n in self.cfg["presets"]:
            self.load_state(self.cfg["presets"][n])
            self.schedule()

    def p_delete(self, *_):
        n = self.pcombo.get_active_text()
        if n in self.cfg["presets"]:
            del self.cfg["presets"][n]
            save_cfg(self.cfg)
            self.refresh_presets()


# ---------------------------------------------------------------- command line (polybar / rofi / hotkeys)
CACHE_DIR = os.path.expanduser("~/.cache/meross-bulb")
CACHE = os.path.join(CACHE_DIR, "state.json")
NAMED = {"red": (0, 100), "orange": (30, 100), "yellow": (55, 100), "green": (120, 100),
         "cyan": (180, 100), "blue": (240, 100), "purple": (280, 100), "pink": (320, 100)}


def cache_load():
    try:
        with open(CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cache_put(ip, st):
    try:
        d = cache_load()
        d[ip] = {"t": time.time(), "st": dict(st)}
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = f"{CACHE}.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, CACHE)
    except OSError:
        pass


def get_state(ip, key, max_age, timeout=2):
    """Cached state if younger than max_age seconds, otherwise ask the bulb (one request)."""
    c = cache_load().get(ip)
    if c and time.time() - c["t"] <= max_age:
        return dict(c["st"])
    st = light_to_state(read_state(ip, key, timeout), c["st"] if c else DEFAULT_STATE)
    cache_put(ip, st)
    return st


def plain_line(st):
    if not st["on"]:
        return "off"
    look = f"color h{st['hue']} s{st['sat']}" if st["mode"] == "rgb" else f"white {st['temp']}"
    return f"on {st['lum']}% {look}"


def polybar_line(st, icon):
    if not st["on"]:
        return f"%{{F#707880}}{icon}%{{F-}} off"
    if st["mode"] == "rgb":
        r, g, b = colorsys.hsv_to_rgb(st["hue"] / 360, st["sat"] / 100, 1.0)
    else:
        r, g, b = 1.0, 0.89, 0.72
    return f"%{{F#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}}}{icon}%{{F-}} {st['lum']}%"


def parse_color(v):
    v = v.strip().lower()
    if v in NAMED:
        return NAMED[v]
    h = v.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"unknown color '{v}' (use {', '.join(NAMED)} or #rrggbb)")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hh, ss, _ = colorsys.rgb_to_hsv(r, g, b)
    return round(hh * 360), round(ss * 100)


def rofi_mode(a, cfg):
    pre = (["-b", a.bulb] if a.bulb else []) + (["--all"] if a.all else [])
    sel = a.selection
    if sel is None:  # rofi asks for the list
        c = cache_load().get(cfg["devices"][0]["ip"]) if cfg["devices"] else None
        head = "\x00prompt\x1fMeross\x1fno-custom\x1ftrue"
        if c:
            head += "\x1fmessage\x1f" + plain_line(c["st"])
        rows = ["Toggle power", "Brightness +10", "Brightness -10", "Brightness 100%",
                "Brightness 50%", "Brightness 10%"]
        rows += [f"Color: {n}" for n in NAMED] + ["White"]
        rows += [f"Preset: {n}" for n in sorted(cfg["presets"])]
        print("\n".join([head] + rows))
        return
    if sel == "Toggle power":
        args = ["toggle"]
    elif sel.startswith("Brightness "):
        args = ["bright", sel.split()[1].rstrip("%")]
    elif sel.startswith("Color: "):
        args = ["color", sel[7:]]
    elif sel == "White":
        args = ["white"]
    elif sel.startswith("Preset: "):
        args = ["preset", sel[8:]]
    else:
        return
    cli(pre + args)


def cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="meross_bulb.py",
                                 description="Local LAN control of Meross bulbs. Run with no arguments for the GUI.")
    ap.add_argument("-b", "--bulb", help="bulb name or IP (default: the first configured bulb)")
    ap.add_argument("--all", action="store_true", help="act on every configured bulb")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="print the bulb's state")
    s.add_argument("--polybar", action="store_true", help="polybar-formatted output")
    s.add_argument("--icon", default="\u25cf", help="glyph used with --polybar")
    s.add_argument("--max-age", type=int, default=30, help="seconds before the bulb is queried again")
    for n, h in (("toggle", "toggle power"), ("on", "power on"), ("off", "power off"),
                 ("presets", "list saved preset names")):
        sub.add_parser(n, help=h)
    sub.add_parser("bright", help="brightness: N, +N or -N (1-100)").add_argument("value")
    sub.add_parser("color", help="color name or #rrggbb").add_argument("value")
    w = sub.add_parser("white", help="white mode with temperature 1-100")
    w.add_argument("value", nargs="?", type=int, default=50)
    sub.add_parser("preset", help="apply a saved preset").add_argument("name")
    sub.add_parser("rofi", help="Rofi script-mode menu").add_argument("selection", nargs="?")
    a = ap.parse_args(argv)

    cfg = load_cfg()
    key, devs = cfg["key"], cfg["devices"]
    if a.cmd == "presets":
        print("\n".join(sorted(cfg["presets"])))
        return
    if a.cmd == "rofi":
        return rofi_mode(a, cfg)
    if not devs:
        sys.exit("No bulbs configured: open the app and use Add or Scan first.")
    if a.all:
        ips = [d["ip"] for d in devs]
    elif a.bulb:
        ips = [d["ip"] for d in devs if a.bulb in (d["ip"], d["name"])][:1]
        if not ips:
            sys.exit(f"No configured bulb called '{a.bulb}'.")
    else:
        ips = [devs[0]["ip"]]

    if a.cmd == "status":
        try:
            st = get_state(ips[0], key, a.max_age)
        except Exception as e:
            if a.polybar:
                print(f"%{{F#707880}}{a.icon} offline%{{F-}}")
                return
            sys.exit(f"{ips[0]}: {e}")
        print(polybar_line(st, a.icon) if a.polybar else plain_line(st))
        return

    preset = None
    if a.cmd == "preset":
        names = {k.lower(): k for k in cfg["presets"]}
        if a.name.lower() not in names:
            sys.exit(f"No preset '{a.name}'. Saved: {', '.join(sorted(names.values())) or 'none'}")
        preset = cfg["presets"][names[a.name.lower()]]

    import fcntl  # serialise bursts (e.g. mouse-wheel events) so they apply in order
    os.makedirs(CACHE_DIR, exist_ok=True)
    lock = open(os.path.join(CACHE_DIR, "lock"), "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    errs = []
    try:
        want_on = None
        if a.cmd == "toggle":
            try:
                want_on = not get_state(ips[0], key, 5)["on"]
            except Exception as e:
                sys.exit(f"{ips[0]}: {e}")
        for ip in ips:
            try:
                st = dict(preset) if preset else get_state(ip, key, 5)
                if a.cmd == "toggle":
                    st["on"] = want_on
                elif a.cmd in ("on", "off"):
                    st["on"] = a.cmd == "on"
                elif a.cmd == "bright":
                    n = st["lum"] + int(a.value) if a.value[0] in "+-" else int(a.value)
                    st.update(lum=max(1, min(100, n)), on=True)
                elif a.cmd == "color":
                    h, sa = parse_color(a.value)
                    st.update(mode="rgb", hue=h, sat=sa, on=True)
                elif a.cmd == "white":
                    st.update(mode="temp", temp=max(1, min(100, a.value)), on=True)
                apply_state(ip, key, st)
                cache_put(ip, st)
            except Exception as e:
                errs.append(f"{ip}: {e}")
    finally:
        lock.close()
    if errs:
        sys.exit("; ".join(errs))


if __name__ == "__main__":
    if CLI_MODE:
        cli(sys.argv[1:])
    elif "--get-key" in sys.argv:
        i = sys.argv.index("--get-key")
        api = sys.argv[sys.argv.index("--api") + 1] if "--api" in sys.argv else "https://iotx-eu.meross.com"
        cli_get_key(sys.argv[i + 1], api)
    else:
        App()
        Gtk.main()
