# Meross Bulbs (local control for Linux Mint)

A small GTK3 app, command-line tool, Rofi menu and polybar module for controlling Meross smart RGB bulbs
**over your LAN**. No cloud is used at runtime. The Meross cloud is contacted once, only to fetch your account key.

![image](https://github.com/user-attachments/assets/17fa3b4d-17a3-4ca1-85b1-7cc8b61ba6a3) 

## Features

- Power, brightness (1-100 %), colour wheel, hue and saturation sliders, quick swatches, colour picker
- White mode with a warm-to-cool temperature slider
- Saved presets, plus a "Control all bulbs" option
- Info tab: model, firmware, Wi-Fi signal, cloud-link status, supported features and raw JSON
- CLI for hotkeys and scripts, a Rofi menu, and a polybar module, all from the same script

## Files

| File | Purpose |
|---|---|
| `meross_bulb.py` | The app (GUI + CLI + Rofi mode) |
| `install.sh` | Adds a launcher and icon so the app appears in Rofi / the Cinnamon menu |
| `polybar-meross.ini` | Polybar module to paste into your polybar config |

## 1. Install dependencies

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 python3-requests
```

## 2. Get your Meross key (one time)

Local commands are signed with your Meross account **key**. You only need to fetch it once.

```bash
cd ~/Documents/gitclones/Meross          # wherever meross_bulb.py lives
python3 -m venv --system-site-packages venv
venv/bin/pip install meross_iot
venv/bin/python meross_bulb.py --get-key you@example.com
```

- You are prompted for your Meross password. It is not stored.
- The key is saved to `~/.config/meross-bulb/config.json` (permissions `600`).
- The default server is EU. For other regions add `--api`:
  - US: `--api https://iotx-us.meross.com`
  - Asia-Pacific: `--api https://iotx-ap.meross.com`
- Run this **once**. Repeated cloud logins can be rate-limited by Meross.
- Alternative: paste a key you already have into the app's **Key** button.
- Bulbs that were never bound to a Meross account can use an empty key.

The key is account-wide, so it works for every bulb on the account. Treat it like a password and don't
commit `config.json` to git.

## 3. Run the app

```bash
/usr/bin/python3 meross_bulb.py
```

(The `venv` is only needed for `--get-key`. If you run the app from a venv, create it with
`--system-site-packages` or you'll get `No module named 'gi'`.)

### Add your bulbs

- **Scan**: probes your local /24 network for Meross devices and adds them (this can also list plugs, so use **Remove** for extras).
- **Add**: enter a name and the bulb's IP address.

Give each bulb a fixed IP (DHCP reservation in your router) so saved addresses stay valid.

### Using it

- **Control tab**: power switch, brightness, colour wheel (click or drag), hue/saturation, swatches, colour picker.
  Choose **White** for temperature mode.
- **Presets**: type a name and press *Save current as preset*. Pick one and press *Apply*.
- **Control all bulbs**: sends every change to all configured bulbs.
- **Info tab**: diagnostics for the selected bulb. Refresh manually or tick auto-refresh (10 s, only while the tab is open).
  The raw output contains your bulb's MAC and Meross user ID, so redact it before sharing.

## 4. Add it to Rofi / the app menu

Put `install.sh` next to `meross_bulb.py`, then:

```bash
bash install.sh
rofi -show drun -show-icons      # type "meross"
```

The launcher records the current folder, so re-run `install.sh` if you move it.
Remove it with `bash install.sh --remove`.

## 5. Command line

Run with no arguments for the GUI. With a subcommand it runs headless (no display needed):

```text
meross_bulb.py status [--polybar] [--icon GLYPH] [--max-age SECONDS]
meross_bulb.py toggle | on | off
meross_bulb.py bright 50 | +10 | -10
meross_bulb.py color red | orange | yellow | green | cyan | blue | purple | pink | #rrggbb
meross_bulb.py white [1-100]
meross_bulb.py preset "Movie"
meross_bulb.py presets
meross_bulb.py rofi [SELECTION]
```

Options go before the subcommand: `-b NAME_OR_IP` picks a bulb (default: the first configured one) and
`--all` targets every bulb. Example: `meross_bulb.py --all preset Movie`.

Bind these to Cinnamon keyboard shortcuts (System Settings, Keyboard, Shortcuts, Custom) using the full
path, e.g. `/usr/bin/python3 /home/you/Documents/gitclones/Meross/meross_bulb.py toggle`.

## 6. Rofi menu (power, brightness, colours, presets)

```bash
rofi -show meross -modi "meross:/usr/bin/python3 /home/you/Documents/gitclones/Meross/meross_bulb.py rofi"
```

Use a path without spaces.

## 7. Polybar

1. Open `polybar-meross.ini` and fix the paths if yours differ.
2. Paste the `[module/meross]` block into `~/.config/polybar/config.ini`.
3. Add `meross` to `modules-left`, `modules-center` or `modules-right`, then restart polybar.

The bar shows a dot in the bulb's current colour plus the brightness. Left click toggles, scroll changes
brightness, right click opens the Rofi menu. It only reads a small cache file every 2 s; the bulb is queried
at most every 30 s.

## Files and data

| Path | Contents |
|---|---|
| `~/.config/meross-bulb/config.json` | Account key, bulbs, presets |
| `~/.cache/meross-bulb/state.json` | Last-known bulb state (used by polybar/CLI) |

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'gi'` | Install the apt packages above and run with `/usr/bin/python3`, or make the venv with `--system-site-packages` |
| Status line shows `bulb error ... (wrong/missing key?)` | Re-run `--get-key`, or set the key with the **Key** button |
| No colour wheel | `sudo apt install python3-gi-cairo` |
| White slider warm/cool is backwards | Set `INVERT_TEMP = True` near the top of `meross_bulb.py` |
| Nothing responds / "offline" | Check the bulb's IP (fixed IP recommended) and that your PC is on the same network/VLAN |
| Not showing in Rofi after install | `rm ~/.cache/rofi3.druncache` and try again |
| Scan finds nothing | The scan only covers your /24; use **Add** with the bulb's IP |

## Notes

- This uses Meross's unofficial local HTTP protocol (`POST http://<bulb-ip>/config`, signed with your key), which may change with firmware.
- The Info tab only sends read-only requests. The app never sends bind, unbind, firmware-upgrade or Wi-Fi config commands.
- The HomeKit setup code is not available locally. Get it in the Meross app under Device settings, Device Info.
