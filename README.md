# SkyRC B6neo 2 Linux flasher

A small PySide6 GUI that flashes firmware to a B6neo 2 over
USB from Linux, using the same HID update protocol the Windows "Charger Master"
tool uses.

## Important

> [!WARNING]
> The flasher was written with help from an LLM, do with this what you want. I tested it on my charger and it flashes successfully.

## Install

The script has inline Python deps, so you can either

```bash
uv run charger_flash.py
```

or:

```bash
pip install PySide6 hidapi
python3 charger_flash.py
```

You'll also need hiadpi system package

```bash
# Debian/Ubuntu:   sudo apt install libhidapi-hidraw0
# Fedora:          sudo dnf install hidapi
# Arch:            sudo pacman -S hidapi
```

## USB permissions (udev)

To run the app as a normal user you need access to the charger
Create `/etc/udev/rules.d/99-skyrc-b6neo2.rules`:

```
# SkyRC B6neo 2 (vid 0000 / pid 0001) — HID update interface
KERNEL=="hidraw*", ATTRS{idVendor}=="0000", ATTRS{idProduct}=="0001", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0000", ATTRS{idProduct}=="0001", MODE="0666"
```

And reload the config

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Get the firmware

Two ways:

- **Download from SkyRC automatically.** Click **Download from SkyRC**. 
  The flasher will download the fw from `upgrade.skyrc.com` itself

- **Manual.** Download the official `*_UPDATEROM.bin` for your charger from
  SkyRC and click **Browse**. (The automatic option above finds the download
  link for you)

## Use

1. Connect the charger (no battery attached).
2. Pick it in **Device**. Click **Detect** to
   confirm it reports core **100225** (B6neo 2), its SN, and firmware version.
3. Get the firmware: **Download from SkyRC** (automatic) or **Browse** to a `.bin`.
4. **Flash**. Don't disconnect while it runs.

## Safety / recovery

The USB **bootloader** lives in a separate, protected flash region that this
process never writes. If a flash fails or is aborted, the charger stays in
bootloader mode — just run the flasher again (or the official Windows tool)
with a good `.bin`. It is very hard to brick permanently this way.

## How it works (protocol)

USB HID, 64-byte reports, report id 0:

| Step | Bytes (after report id) | Meaning |
|------|-------------------------|---------|
| identify | `0F 03 57 01 58` | reply contains ASCII core id ("100225") |
| enter bootloader | `0F 02 60 60` | then wait ~4 s and reopen |
| info | `00` | handshake start |
| start | `01` + len(u32 BE) + sum(u32 BE) | `sum` = plain 32-bit byte sum of the image |
| block | `02` + offset(u32 BE) + len(u16 BE) | `len` ≤ 1024 |
| data | `03` + 31 bytes | 34 packets per block |
| finish | `04` | reply ready byte == 1 on success |
| run | `05` | boot the new firmware |

This was reverse-engineered from Charger Master V1.55.
