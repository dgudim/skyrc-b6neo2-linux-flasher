#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "PySide6>=6.6",
#     "hidapi>=0.14",
# ]
# ///
"""
SkyRC B6neo 2 firmware flasher for Linux (PySide6)
==================================================

A small GUI that flashes OFFICIAL SkyRC firmware (the *_UPDATEROM.bin files you
download from http://upgrade.skyrc.com/) to a SkyRC B6neo 2 charger over USB,
using the same HID update protocol the Windows "Charger Master" tool uses.

It can also auto-detect the charger's SN and download the matching official
firmware from SkyRC's own update server (the same request the Windows tool
makes), or you can point it at a .bin you already have.

Protocol summary (reverse-engineered from Charger Master V1.55, core 100225):
  USB HID, 64-byte reports, report id 0.
  Device identity query : 0F 03 57 01 58   -> reply contains ASCII core id
  Enter bootloader       : 0F 02 60 60       (then wait ~4 s)
  cmd 0  info
  cmd 1  start   : [01][len u32 BE][sum u32 BE]      sum = plain byte sum
  cmd 2  block   : [02][offset u32 BE][len u16 BE]   len <= 1024
  cmd 3  data    : [03][31 payload bytes]            34 per block (1054 >= 1024)
  cmd 4  finish  : [04]   -> success when reply ready byte == 1
  cmd 5  run application
"""

import sys
import os
import time
import json
import struct
import shutil
import tempfile
import subprocess
import urllib.request


def _system_qt6_plugin_dir():
    """Locate the distro's Qt6 plugin directory (where Breeze lives)."""
    for tool in ("qtpaths6", "qtpaths-qt6"):
        exe = shutil.which(tool)
        if exe:
            try:
                out = subprocess.run([exe, "--query", "QT_INSTALL_PLUGINS"],
                                     capture_output=True, text=True, timeout=5)
                p = out.stdout.strip()
                if p and os.path.isdir(p):
                    return p
            except Exception:
                pass
    for p in ("/usr/lib/qt6/plugins", "/usr/lib64/qt6/plugins",
              "/usr/lib/x86_64-linux-gnu/qt6/plugins",
              "/usr/lib/aarch64-linux-gnu/qt6/plugins"):
        if os.path.isdir(p):
            return p
    return None


def setup_system_theme():
    """On KDE, point Qt at the system Breeze style/platform theme before the
    QApplication is built. Only acts on KDE and never overrides vars you set.
    If the bundled Qt and system Qt minor versions differ, Breeze simply won't
    load and Qt falls back to Fusion -- harmless."""
    if sys.platform != "linux":
        return
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP", "") + ":" +
               os.environ.get("XDG_SESSION_DESKTOP", "")).upper()
    if "KDE" not in desktop and not os.environ.get("KDE_FULL_SESSION"):
        return
    plug = _system_qt6_plugin_dir()
    if plug:
        existing = [p for p in os.environ.get("QT_PLUGIN_PATH", "").split(os.pathsep) if p]
        merged = list(dict.fromkeys([plug] + existing))
        os.environ["QT_PLUGIN_PATH"] = os.pathsep.join(merged)
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "kde")
    os.environ.setdefault("QT_STYLE_OVERRIDE", "Breeze")


# Must run before PySide6 builds the QApplication (Qt reads these at startup).
setup_system_theme()

from PySide6 import QtCore, QtGui, QtWidgets

try:
    import hid  # cython-hidapi: `pip install hidapi`
except Exception:  # pragma: no cover
    hid = None

# ----------------------------------------------------------------------------
# Device / protocol constants
# ----------------------------------------------------------------------------
SKYRC_VID = 0x0000          # B6neo 2 reports a zero vendor id (unusual but real)
SKYRC_PID = 0x0001
REPORT_LEN = 64             # HID report payload size (excludes the report-id byte)
B6NEO2_CORE = "100225"      # core id string the charger returns for a B6neo 2

BLOCK = 1024                # bytes per cmd-2 block
CHUNK = 31                  # payload bytes per cmd-3 data packet
PKTS_PER_BLOCK = 34         # 34 * 31 = 1054 >= 1024, matches the official tool

# SkyRC update server. The charger's SN (machine_id) is the hex of the 15
# identity bytes plus a trailing two's-complement checksum byte. The server
# answers a JSON array of releases; we pick the highest version.
UPGRADE_URL = "http://upgrade.skyrc.com/?SN={sn}&test=0"

# Official core-100225 images, keyed by (length, byte-sum) -> version.
# Used only to reassure you that the file you picked is a genuine release.
KNOWN_IMAGES = {
    (327476, 29968726): "1.21",
    (272012, 25824567): "1.19",
    (276596, 26157264): "1.18",
    (275788, 26081596): "1.17",
    (275720, 26079296): "1.16",
}


def byte_sum(data: bytes) -> int:
    return sum(data) & 0xFFFFFFFF


def machine_id_from(id15: bytes) -> str:
    """Build the server SN: hex of the 15 identity bytes + two's-complement
    checksum byte, exactly as the Windows tool does."""
    chk = (-sum(id15)) & 0xFF
    return (id15 + bytes([chk])).hex()


# ----------------------------------------------------------------------------
# Thin HID wrapper
# ----------------------------------------------------------------------------
class HidLink:
    """Open the charger by path (preferred) or by VID/PID, and do report I/O."""

    def __init__(self, path: bytes | None = None,
                 vid: int = SKYRC_VID, pid: int = SKYRC_PID):
        self.path = path
        self.vid = vid
        self.pid = pid
        self.dev = None

    @staticmethod
    def candidates():
        """Return a list of (label, path, vid, pid) for all HID devices,
        charger-looking ones first."""
        if hid is None:
            return []
        out = []
        for d in hid.enumerate():
            vid, pid = d.get("vendor_id", 0), d.get("product_id", 0)
            name = (d.get("product_string") or "").strip()
            man = (d.get("manufacturer_string") or "").strip()
            label = f"{vid:04x}:{pid:04x}"
            if name or man:
                label += f"  {man} {name}".rstrip()
            out.append((label, d.get("path"), vid, pid))
        out.sort(key=lambda t: (not (t[2] == SKYRC_VID and t[3] == SKYRC_PID),))
        return out

    def open(self):
        self.dev = hid.device()
        if self.path:
            self.dev.open_path(self.path)
        else:
            self.dev.open(self.vid, self.pid)
        try:
            self.dev.set_nonblocking(0)
        except Exception:
            pass

    def close(self):
        try:
            if self.dev:
                self.dev.close()
        finally:
            self.dev = None

    def write(self, payload: bytes):
        """Write one report. A leading 0x00 report-id byte is prepended and the
        buffer padded to REPORT_LEN."""
        buf = bytes([0x00]) + payload
        if len(buf) < REPORT_LEN + 1:
            buf = buf + bytes(REPORT_LEN + 1 - len(buf))
        n = self.dev.write(buf)
        if n < 0:
            raise IOError("HID write failed")

    def read(self, timeout_ms: int = 2000) -> bytes:
        data = self.dev.read(REPORT_LEN, timeout_ms)
        return bytes(data) if data else b""


# ----------------------------------------------------------------------------
# Worker thread: detection and flashing
# ----------------------------------------------------------------------------
class Worker(QtCore.QThread):
    progress = QtCore.Signal(int)
    log = QtCore.Signal(str)
    info = QtCore.Signal(dict)
    done = QtCore.Signal(bool, str)

    releases = QtCore.Signal(list)        # list of {version, url, checksum}
    firmware = QtCore.Signal(bytes, str)  # downloaded image + suggested name

    def __init__(self, mode: str, path: bytes | None, vid: int, pid: int,
                 firmware: bytes | None = None, url: str | None = None,
                 expect: int | None = None, name: str | None = None):
        super().__init__()
        self.mode = mode
        self.path = path
        self.vid = vid
        self.pid = pid
        self.firmware_bytes = firmware
        self.url = url
        self.expect = expect
        self.name = name
        self._abort = False

    def abort(self):
        self._abort = True

    # -- helpers --------------------------------------------------------------
    def _parse_identity(self, r: bytes) -> dict:
        """Find the 6-digit ASCII core id in an identity reply, framing-agnostic,
        and build the 15-byte identity block + server SN from it."""
        res = {}
        for i in range(0, max(0, len(r) - 6)):
            chunk = r[i:i + 6]
            if all(0x30 <= c <= 0x39 for c in chunk):
                res["core"] = chunk.decode("ascii")
                tail = r[i + 6:]
                if len(tail) >= 9:
                    res["upgrade_type"] = tail[0]
                    res["encrypted"] = tail[1] == 1
                    res["sw"] = f"{tail[5]}.{tail[6]:02d}"
                    id15 = r[i:i + 15]
                    if len(id15) == 15:
                        res["id15"] = id15.hex()
                        res["sn"] = machine_id_from(id15)
                break
        return res

    # -- modes ----------------------------------------------------------------
    def run(self):
        if hid is None:
            self.done.emit(False, "The 'hidapi' Python package is not installed "
                                  "(pip install hidapi).")
            return
        try:
            if self.mode == "detect":
                self._detect()
            elif self.mode == "list":
                self._list()
            elif self.mode == "download":
                self._download()
            else:
                self._flash()
        except Exception as e:  # pragma: no cover
            self.done.emit(False, f"{type(e).__name__}: {e}")

    def _detect(self):
        link = HidLink(self.path, self.vid, self.pid)
        self.log.emit("Opening device...")
        link.open()
        try:
            # identity query: 0F 03 57 01 58
            link.write(bytes([0x0F, 0x03, 0x57, 0x01, 0x58]))
            r = link.read(1500)
            if not r:
                self.info.emit({})
                self.done.emit(True, "Device opened, but it did not answer the "
                                     "identity query. It may be in bootloader "
                                     "mode already, or busy. You can still try to "
                                     "flash.")
                return
            ident = self._parse_identity(r)
            ident["raw"] = r[:22].hex()
            self.info.emit(ident)
            core = ident.get("core", "?")
            if core == B6NEO2_CORE:
                self.done.emit(True, f"B6neo 2 detected (core {core}, "
                                     f"fw {ident.get('sw', '?')}).")
            else:
                self.done.emit(True, f"Device answered with core id {core}. "
                                     f"Expected {B6NEO2_CORE} for a B6neo 2 -- "
                                     f"do NOT flash B6neo 2 firmware to a "
                                     f"different model.")
        finally:
            link.close()

    def _expect(self, link: HidLink, want_cmd: int, timeout_ms=2500, tries=4):
        """Read reports until one whose command byte matches `want_cmd`."""
        for _ in range(tries):
            r = link.read(timeout_ms)
            if not r:
                continue
            cmd = r[2] if (len(r) > 2 and r[0] == 0x0F) else r[0]
            if cmd == want_cmd:
                return r
        return None

    def _query_sn(self):
        """Open the device, read identity, return (sn, ident) or (None, {})."""
        link = HidLink(self.path, self.vid, self.pid)
        link.open()
        try:
            link.write(bytes([0x0F, 0x03, 0x57, 0x01, 0x58]))
            r = link.read(1500)
        finally:
            link.close()
        ident = self._parse_identity(r) if r else {}
        return ident.get("sn"), ident

    def _list(self):
        self.log.emit("Reading charger identity...")
        sn, ident = self._query_sn()
        if not sn:
            self.done.emit(False, "Could not read the charger SN. Use Detect, or "
                                  "download the .bin manually.")
            return
        core = ident.get("core", "?")
        self.log.emit(f"Core {core}, SN {sn}, fw {ident.get('sw', '?')}.")
        if core != B6NEO2_CORE:
            self.done.emit(False, f"Connected device reports core {core}, not "
                                  f"{B6NEO2_CORE}. Not fetching B6neo 2 firmware.")
            return
        url = UPGRADE_URL.format(sn=sn)
        self.log.emit(f"Querying {url}")
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
        if not body or body == "[]":
            self.done.emit(False, "The update server returned no firmware for this "
                                  "SN. Download the .bin manually and use Browse.")
            return
        data = json.loads(body)
        rels = []
        for e in data:
            try:
                rels.append({
                    "version": e["version"],
                    "url": e["download_url"],
                    "checksum": int(e["checksum"]),
                    "memo": (e.get("memo") or {}).get("en", ""),
                })
            except (KeyError, ValueError):
                continue
        rels.sort(key=lambda x: float(x["version"]), reverse=True)
        if not rels:
            self.done.emit(False, "Could not parse the server response.")
            return
        self.log.emit(f"Found {len(rels)} release(s): "
                      + ", ".join(r["version"] for r in rels))
        # No done() here: the GUI drives the next step (version choice +
        # download) off the releases signal and manages its own busy state.
        self.releases.emit(rels)

    def _download(self):
        self.log.emit(f"Downloading {self.url}")
        with urllib.request.urlopen(self.url, timeout=60) as resp:
            data = resp.read()
        got = byte_sum(data)
        if self.expect is not None and got != self.expect:
            self.done.emit(False, f"Downloaded {len(data)} bytes but checksum "
                                  f"{got} != expected {self.expect}. Not using it.")
            return
        self.log.emit(f"Downloaded {len(data)} bytes, checksum {got} verified.")
        self.firmware.emit(data, self.name or "firmware.bin")
        self.done.emit(True, "Firmware downloaded and verified.")

    def _flash(self):
        fw = self.firmware_bytes
        length = len(fw)
        csum = byte_sum(fw)
        self.log.emit(f"Image: {length} bytes, byte-sum checksum {csum}.")
        ver = KNOWN_IMAGES.get((length, csum))
        if ver:
            self.log.emit(f"Recognised as official core-100225 firmware v{ver}.")
        else:
            self.log.emit("NOTE: this file does not match a known official "
                          "B6neo 2 release. Make sure it is the correct image "
                          "for this charger before continuing.")

        link = HidLink(self.path, self.vid, self.pid)
        self.log.emit("Opening device...")
        link.open()
        try:
            # 1) enter bootloader
            self.log.emit("Requesting bootloader (0F 02 60 60)...")
            link.write(bytes([0x0F, 0x02, 0x60, 0x60]))
            link.close()
            for s in range(4, 0, -1):
                if self._abort:
                    self.done.emit(False, "Aborted.")
                    return
                self.log.emit(f"Waiting for the charger to re-enter bootloader... {s}")
                time.sleep(1)

            # 2) reopen (the bootloader presents the same VID/PID)
            self.log.emit("Reopening device in bootloader mode...")
            reopened = False
            for _ in range(10):
                try:
                    link.open()
                    reopened = True
                    break
                except Exception:
                    time.sleep(0.5)
            if not reopened:
                self.done.emit(False, "Could not reopen the device after the "
                                      "bootloader request. Replug and retry; the "
                                      "charger is not harmed.")
                return

            # 3) info + start
            self.log.emit("Handshake: info (cmd 0) / start (cmd 1)...")
            link.write(bytes([0x00]))            # cmd 0 info
            link.read(1500)                      # consume reply if any
            start = bytes([0x01]) + struct.pack(">I", length) + struct.pack(">I", csum)
            link.write(start)
            ack = self._expect(link, 0x01, 2500, 5)
            if ack is None:
                self.log.emit("No explicit start ack; proceeding (device may be "
                              "silent until the first block).")

            # 4) block loop
            offset = 0
            while offset < length:
                if self._abort:
                    self.done.emit(False, "Aborted mid-flash. Re-run with a good "
                                          "image to recover; the bootloader is "
                                          "intact.")
                    return
                block = min(BLOCK, length - offset)
                hdr = bytes([0x02]) + struct.pack(">I", offset) + struct.pack(">H", block)
                link.write(hdr)
                if self._expect(link, 0x02, 2500, 4) is None:
                    self.done.emit(False, f"No ack for block at offset {offset}. "
                                          f"Aborting; charger is recoverable.")
                    return
                # send the block as 34 fixed-size data packets
                sent = 0
                for _ in range(PKTS_PER_BLOCK):
                    chunk = fw[offset + sent: offset + min(sent + CHUNK, block)]
                    if len(chunk) < CHUNK:
                        chunk = chunk + bytes(CHUNK - len(chunk))
                    link.write(bytes([0x03]) + chunk)
                    sent = min(sent + CHUNK, block)
                offset += block
                self.progress.emit(int(offset * 100 / length))

            # 5) finish + run
            self.log.emit("Finalising (cmd 4)...")
            link.write(bytes([0x04]))
            fin = self._expect(link, 0x04, 4000, 6)
            ok = True
            if fin is not None:
                ready = fin[3] if (len(fin) > 3 and fin[0] == 0x0F) else (fin[1] if len(fin) > 1 else 1)
                ok = (ready == 1)
            link.write(bytes([0x05]))            # run application
            self.progress.emit(100)
            if ok:
                self.done.emit(True, "Flash complete. The charger should reboot "
                                     "into the new firmware.")
            else:
                self.done.emit(False, "The charger did not confirm success. If it "
                                      "does not boot, re-run the flasher with the "
                                      "same image; the bootloader is intact.")
        finally:
            link.close()


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SkyRC B6neo 2 Flasher (Linux)")
        self.setMinimumWidth(620)
        self.worker: Worker | None = None
        self.fw_bytes: bytes | None = None

        v = QtWidgets.QVBoxLayout(self)

        # Warning banner: a themed panel with the system warning icon, no
        # hardcoded colours, so it follows the Qt system palette (light/dark).
        warn_box = QtWidgets.QFrame()
        warn_box.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        wl = QtWidgets.QHBoxLayout(warn_box)
        warn_icon = QtWidgets.QLabel()
        warn_icon.setPixmap(self.style().standardIcon(
            QtWidgets.QStyle.StandardPixmap.SP_MessageBoxWarning).pixmap(22, 22))
        wl.addWidget(warn_icon, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        warn = QtWidgets.QLabel(
            "Flashes OFFICIAL SkyRC firmware to your B6neo 2 over USB.\n"
            "Do not disconnect the charger while flashing. A failed flash is "
            "recoverable – just run this again with a good .bin."
        )
        warn.setWordWrap(True)
        wl.addWidget(warn, 1)
        v.addWidget(warn_box)

        # device row
        drow = QtWidgets.QHBoxLayout()
        drow.addWidget(QtWidgets.QLabel("Device:"))
        self.dev_combo = QtWidgets.QComboBox()
        drow.addWidget(self.dev_combo, 1)
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.detect_btn = QtWidgets.QPushButton("Detect")
        drow.addWidget(self.refresh_btn)
        drow.addWidget(self.detect_btn)
        v.addLayout(drow)

        # firmware row
        frow = QtWidgets.QHBoxLayout()
        frow.addWidget(QtWidgets.QLabel("Firmware:"))
        self.fw_edit = QtWidgets.QLineEdit()
        self.fw_edit.setReadOnly(True)
        self.fw_edit.setPlaceholderText("Select a *_UPDATEROM.bin file...")
        frow.addWidget(self.fw_edit, 1)
        self.download_btn = QtWidgets.QPushButton("Download from SkyRC")
        self.browse_btn = QtWidgets.QPushButton("Browse…")
        frow.addWidget(self.download_btn)
        frow.addWidget(self.browse_btn)
        v.addLayout(frow)

        self.fw_info = QtWidgets.QLabel("")
        self.fw_info.setWordWrap(True)
        v.addWidget(self.fw_info)

        self.progress = QtWidgets.QProgressBar()
        v.addWidget(self.progress)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(180)
        v.addWidget(self.log, 1)

        brow = QtWidgets.QHBoxLayout()
        brow.addStretch(1)
        self.flash_btn = QtWidgets.QPushButton("Flash")
        self.flash_btn.setEnabled(False)
        self.abort_btn = QtWidgets.QPushButton("Abort")
        self.abort_btn.setEnabled(False)
        brow.addWidget(self.abort_btn)
        brow.addWidget(self.flash_btn)
        v.addLayout(brow)

        self.refresh_btn.clicked.connect(self.refresh_devices)
        self.detect_btn.clicked.connect(self.detect)
        self.browse_btn.clicked.connect(self.browse)
        self.download_btn.clicked.connect(self.download_online)
        self.flash_btn.clicked.connect(self.flash)
        self.abort_btn.clicked.connect(self.abort)

        app = QtWidgets.QApplication.instance()
        style = app.style().objectName() if app else "?"
        self._log(f"Qt style: {style}  (set QT_STYLE_OVERRIDE to change)")
        if style == "fusion":
            self._log("Note: Breeze did not load -- usually the pip/uv PySide6 "
                      "Qt version differs from your system Qt. Running with your "
                      "distro's python3-pyside6 gives native Breeze.")
        if hid is None:
            self._log("The 'hidapi' package is missing. Run: pip install hidapi")
        self.refresh_devices()

    # -- ui helpers -----------------------------------------------------------
    def _log(self, msg: str):
        self.log.appendPlainText(msg)

    def _selected_device(self):
        idx = self.dev_combo.currentIndex()
        return self.dev_combo.itemData(idx) if idx >= 0 else None

    def refresh_devices(self):
        self.dev_combo.clear()
        for label, path, vid, pid in HidLink.candidates():
            self.dev_combo.addItem(label, (path, vid, pid))
        if self.dev_combo.count() == 0:
            self._log("No HID devices found. Check the udev rule and that the "
                      "charger is connected.")

    def _accent(self, ok: bool) -> str:
        """Green/amber accent that stays legible on the current palette."""
        dark = self.palette().color(
            QtGui.QPalette.ColorRole.Window).lightness() < 128
        if ok:
            return "#81c784" if dark else "#2e7d32"
        return "#ffb74d" if dark else "#b26a00"

    def _load_firmware_bytes(self, data: bytes, label: str):
        self.fw_bytes = data
        self.fw_edit.setText(label)
        length = len(data)
        csum = byte_sum(data)
        ver = KNOWN_IMAGES.get((length, csum))
        if ver:
            self.fw_info.setText(
                f"{length} bytes – checksum {csum} – "
                f"official B6neo 2 firmware v{ver} ✓")
            self.fw_info.setStyleSheet(f"color:{self._accent(True)};")
        else:
            self.fw_info.setText(
                f"{length} bytes – checksum {csum} – "
                f"not a recognised official B6neo 2 release. Double-check this is "
                f"the right image.")
            self.fw_info.setStyleSheet(f"color:{self._accent(False)};")
        self.flash_btn.setEnabled(True)

    def browse(self):
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select firmware", "", "Firmware (*.bin);;All files (*)")
        if not fn:
            return
        try:
            with open(fn, "rb") as f:
                data = f.read()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", str(e))
            return
        self._load_firmware_bytes(data, fn)

    def download_online(self):
        dev = self._selected_device()
        if not dev:
            return
        path, vid, pid = dev
        self._set_busy(True)
        self._log("Checking SkyRC for firmware (reads the charger SN first)...")
        self.worker = Worker("list", path, vid, pid)
        self.worker.log.connect(self._log)
        self.worker.releases.connect(self._on_releases)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def _on_releases(self, rels: list):
        labels = [f"v{r['version']}" + (f"  ({r['memo']})" if r['memo'] else "")
                  for r in rels]
        choice, ok = QtWidgets.QInputDialog.getItem(
            self, "Choose firmware",
            "Official releases for this charger (newest first):",
            labels, 0, False)
        if not ok:
            self._set_busy(False)
            return
        rel = rels[labels.index(choice)]
        dev = self._selected_device()
        _, vid, pid = dev
        name = os.path.basename(rel["url"].split("?")[0]) or f"V{rel['version']}.bin"
        self._set_busy(True)
        self.worker = Worker("download", None, vid, pid,
                             url=rel["url"], expect=rel["checksum"], name=name)
        self.worker.log.connect(self._log)
        self.worker.firmware.connect(self._on_firmware)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def _on_firmware(self, data: bytes, name: str):
        # cache a copy next to the script so the user keeps the .bin
        try:
            cache = os.path.join(tempfile.gettempdir(), name)
            with open(cache, "wb") as f:
                f.write(data)
            label = cache
        except Exception:
            label = name
        self._load_firmware_bytes(data, label)

    def _set_busy(self, busy: bool):
        for w in (self.flash_btn, self.detect_btn, self.browse_btn,
                  self.download_btn, self.refresh_btn, self.dev_combo):
            w.setEnabled(not busy)
        self.abort_btn.setEnabled(busy)
        if not busy and self.fw_bytes is None:
            self.flash_btn.setEnabled(False)

    def detect(self):
        dev = self._selected_device()
        if not dev:
            return
        path, vid, pid = dev
        self._set_busy(True)
        self.worker = Worker("detect", path, vid, pid)
        self.worker.log.connect(self._log)
        self.worker.info.connect(self._on_info)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def _on_info(self, ident: dict):
        if ident:
            self._log("Identity: " + ", ".join(f"{k}={v}" for k, v in ident.items()))

    def flash(self):
        dev = self._selected_device()
        if not dev or self.fw_bytes is None:
            return
        path, vid, pid = dev
        ver = KNOWN_IMAGES.get((len(self.fw_bytes), byte_sum(self.fw_bytes)), "UNKNOWN")
        msg = (f"Flash this firmware (v{ver}) to the selected charger?\n\n"
               "• Make sure the device is a B6neo 2 (use Detect first).\n"
               "• Do not disconnect during flashing.\n"
               "• If it fails, re-run with a good .bin to recover.")
        if QtWidgets.QMessageBox.question(self, "Confirm flash", msg) != \
                QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.progress.setValue(0)
        self._set_busy(True)
        self.worker = Worker("flash", path, vid, pid, self.fw_bytes)
        self.worker.log.connect(self._log)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def abort(self):
        if self.worker:
            self._log("Abort requested...")
            self.worker.abort()

    def _on_done(self, ok: bool, msg: str):
        self._log(("OK: " if ok else "FAILED: ") + msg)
        self._set_busy(False)
        if not ok:
            QtWidgets.QMessageBox.warning(self, "Result", msg)


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
