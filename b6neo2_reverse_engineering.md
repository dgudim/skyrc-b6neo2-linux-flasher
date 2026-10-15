# SkyRC B6neo 2 — Charger Master V1.55 reverse-engineering notes

Notes from reverse-engineering the official SkyRC "Charger Master" V1.55
updater and the B6neo 2 (core `100225`) firmware.

Big thanks to amazing folks at https://budgetlightforum.com/t/skyrc-mc3000-reverse-engineering-the-firmware-update-program-and-extracting-the-firmware-embedded-in-it/220274/9

## Installer
- `charger_master_V1_55.exe` = Advanced Installer SFX. File table at end of file
  (before `ADVINSTSFX`): entries are `size(u32) offset(u32) namelen(u32) name(UTF-16)`.
- Payloads `FILES.7z` and `charger master.7z` have the first 0x200 bytes
  bitwise-inverted (XOR 0xFF); after un-inverting they are normal 7z archives.
- All apps are .NET Framework 4.5 (decompile cleanly with ILSpy; no obfuscation).

## App layout
- `Charger Master.exe` = launcher. Talks USB HID **VID 0x0000 / PID 0x0001**,
  sends cmd 0x57, reads core ID, then starts the right sub-app per
  `Charger Master_CoreType.xml`.
- **B6neo 2 = core ID `100225`** → `Charger Master T1000\Charger Master_E.exe`
  (namespace `ChargeMaster3`). It is in `upgradeModel` only: the PC app can only
  update firmware on it, not control charging.

## Device ID (cmd 0x57 reply, bytes 6..20)
```
core_type(6 ASCII) | upgrade_type | is_encrypt | customer_id(u16 BE) |
language_id | sw_major | sw_minor | hw_ver | reserved
```
`machine_id` (the "SN" the update server wants) = hex of those 15 bytes + a
trailing checksum byte (two's-complement of their sum). The app shows it as
"PID:" on the System page. For the B6neo 2, `is_encrypt` is 0 (firmware ships
in the clear).

## Firmware download
- `GET http://upgrade.skyrc.com/?SN=<machine_id>&test=0` → JSON array of
  `{id, timestamp, version, memo{cn,en,tw,jp}, force, checksum, download_url}`;
  the app takes the highest `version` and downloads `download_url` to
  `%TEMP%\SKYRC_Firmware`. `checksum` is the plain 32-bit byte sum of the image
  (decimal).
- "Repair" mode rewrites the SN's sw-version bytes to 01 00 (v1.00) and
  recomputes the checksum byte, so the server returns the latest image
  regardless of the installed version.
- Released images seen for core 100225 (lengths/sums are public firmware
  integrity values; download URLs are per-version on `upgrade.skyrc.com` and
  are returned by the query above):

  | Ver  | Date (UTC) | Notes                        | Length | Byte-sum   |
  |------|-----------|-------------------------------|--------|------------|
  | 1.21 | 2026-08-10 | UI tweaked                   | 327476 | 29968726   |
  | 1.19 | 2026-07-27 | UI tweaked                   | 272012 | 25824567   |
  | 1.18 | 2026-06-30 | Charging optimized           | 276596 | 26157264   |
  | 1.17 | 2026-05-27 | Display optimized            | 275788 | 26081596   |
  | 1.16 | 2026-01-26 | PD current output optimized  | 275720 | 26079296   |

- Only cores 100182/100185 use Intel HEX; everything else (incl. 100225) is a
  raw `.bin` sent as-is. If `is_encrypt`=1, decryption happens in the device
  bootloader (the PC never sees plaintext).

## USB bootloader protocol (64-byte HID reports, report ID 0)
- Enter bootloader: `0F 02 60 60` (wait ~4 s).
- `00` info → `01` start: len u32 BE, checksum u32 BE (plain byte sum of image);
  device echoes the first 9 bytes.
- `02` block: offset u32 BE, len u16 BE (≤1024); then 34× `03` packets with 31
  data bytes each (bytes 2..32).
- `04` finish → device replies `04 01 00` on success; `05` = run application.
- Integrity check is only a 32-bit byte sum; no signature visible on the PC side.
  The bootloader lives below the application region, so reflashing the app is
  recoverable by re-sending an official image.

## Firmware image analysis (v1.21; also v1.16–1.19)
- **Not encrypted**: plain Cortex-M (Thumb-2) image.
- Load address **0x0800C000** (48 KB bootloader below it). Image ends
  ~0x0805C000 → ≥512 KB flash. Initial SP 0x20015CD0.
- MCU: STM32F1-style peripheral map (RCC 0x40021000, GPIOA–G, ADC1/2, TIM1/8,
  I2C1) + USB OTG at 0x50000000 and >256 KB flash → most likely a
  **GD32F305/F307** (STM32F105/107-compatible). Built with Keil MDK; UI uses LVGL.
- Keil scatter-load: region table at 0x0805B16C; RW data compressed (Keil
  `__decompress1`) at 0x0805B18C → 0x20004000, 0x1758 bytes; zero-init
  0x20005758 len 0x10578. UI string tables (all languages) live in that RW data.

### Balance-related functions (v1.21)
- `FUN_08022E00` — ADC scan/filter: cell_mV = 0.5·old + 0.5·raw·cal; skips cell
  channels while bleeding (flag 0x20004070).
- `FUN_08014FE8` — cell count = consecutive cells > 2.5 V; sets balance-lead flag.
- `FUN_080150D4` — sum / max / min cell; returns index of lowest cell.
- `FUN_08025424(mask)` — bleed FET driver: bit0 PC4, bit1 PC5, bit2 PB0,
  bit3 PB11, bit4 PB2, bit5 PB13 (set = bleed on).
- `FUN_0800E094` — balancer. 101-slot cycle (one slot per 11 calls). Slots
  0..[+0x96]: bleed off, cells measured, new mask computed. Remaining slots:
  mask applied, cell ADC paused. Modes: 1 = bleed cells ≥ min+8 mV (or
  > target+8); 2 = bleed cells > target, off below target−4 (storage/discharge);
  5 = factory test.
- `FUN_0800E3CC` — bleed duty: X = I_chg·7.5/maxcell·100, clamped 10–90 %;
  forced 90 % if max−min > 30 mV. Thermal derate: above 60.0 °C internal, duty
  drops 1 % per 0.3 °C (floor 10 %).
- `FUN_0801B26C` — Li charge controller. Sets balance mode 1 if lead present. CV
  regulates the **highest cell** to target+5 mV by stepping current ±5 mA (floor
  ≈105 mA). Over-voltage fault if min > target+70 or max > target+120.
- `FUN_08010828` — end-of-charge: done when min ≥ target & max < target+10
  (current <200 mA, debounced), or min > target−5 & max < target+10 (long
  debounce); max > target+20 → over-voltage state 5.
- `FUN_0800D134` — discharge/storage controller; sets balance mode 2 if lead
  present.
