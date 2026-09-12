"""
Identify the connected Apple audio device and pick its loudness reference.

Windows exposes the Bluetooth vendor and product ID of every paired device in
the registry under BTHENUM. Apple's vendor ID is 0x004C, and the product ID
names the model. Reading it lets the meter use a per-model reference instead
of one number for every headphone.

PID table source: https://theapplewiki.com/wiki/Bluetooth_PIDs
"""

import json
import os
import re
import winreg

APPLE_VID = "004C"

# Product ID -> marketing name.
MODELS = {
    0x2002: "AirPods (1st generation)",
    0x2003: "Powerbeats 3",
    0x2005: "BeatsX",
    0x2006: "Beats Solo3 Wireless",
    0x2009: "Beats Studio3 Wireless",
    0x200A: "AirPods Max (Lightning)",
    0x200B: "Powerbeats Pro",
    0x200C: "Beats Solo Pro",
    0x200D: "Powerbeats (4th generation)",
    0x200E: "AirPods Pro (1st generation)",
    0x200F: "AirPods (2nd generation)",
    0x2010: "Beats Flex",
    0x2011: "Beats Studio Buds",
    0x2012: "Beats Fit Pro",
    0x2013: "AirPods (3rd generation)",
    0x2014: "AirPods Pro (2nd generation)",
    0x2016: "Beats Studio Buds +",
    0x2017: "Beats Studio Pro",
    0x201A: "Beats Pill 3",
    0x201B: "AirPods 4",
    0x201F: "AirPods Max (USB-C)",
    0x2024: "AirPods Pro (2nd generation, USB-C)",
    0x2025: "Beats Solo 4",
    0x2026: "Beats Solo Buds",
    0x202D: "AirPods Max 2",
}

# Product IDs seen in the wild but not yet in the published table. Inferred
# from the release window and where the ID falls in the range, so the name is
# a best guess; the loudness reference still comes from the family.
PROVISIONAL = {
    0x2027: "AirPods Pro (3rd generation)",
}

# Reference SPL in dB(A) produced by a 0 dBFS RMS signal at 100 % volume.
#
# This is the one number the whole estimate rests on, and it is the weakest
# link. Only a handful of models have been measured publicly; the rest inherit
# a family value. Anything marked "estimated" is a starting point, not a fact.
#
#   measured  - published coupler measurement
#   checked   - family value cross-checked against iOS Health on one unit
#   estimated - family value, unverified
DEFAULT_REFERENCE = 104.0

REFERENCES = {
    0x2013: (105.7, "measured"),    # AirPods 3, MiniDSP H.E.A.R.S, white noise
    0x200A: (108.3, "measured"),    # AirPods Max
    0x201F: (108.3, "estimated"),
    0x202D: (108.3, "estimated"),
    0x2002: (105.0, "estimated"),
    0x200F: (105.0, "estimated"),
    0x201B: (105.0, "estimated"),
    0x200E: (104.0, "estimated"),
    0x2014: (104.0, "checked"),
    0x2024: (104.0, "checked"),
}

FAMILIES = (
    ("AirPods Max", 108.3),
    ("AirPods Pro", 104.0),
    ("AirPods", 105.0),
    ("Beats", 105.0),
)


class Device:
    def __init__(self, name, pid=None, model=None, reference=DEFAULT_REFERENCE,
                 confidence="default"):
        self.name = name                # what the user called it
        self.pid = pid                  # Bluetooth product ID, or None
        self.model = model              # marketing name, or None if unknown
        self.reference = reference      # dB(A) at 0 dBFS, full volume
        self.confidence = confidence

    @property
    def identified(self):
        return self.model is not None

    def __repr__(self):
        pid = "0x{:04X}".format(self.pid) if self.pid else "-"
        return "<Device {!r} pid={} model={!r} ref={:.1f} ({})>".format(
            self.name, pid, self.model, self.reference, self.confidence)


def _registry_devices():
    """[(pid, device name)] for every paired Apple Bluetooth audio device."""
    out = []
    root = r"SYSTEM\CurrentControlSet\Enum\BTHENUM"
    try:
        hive = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
    except OSError:
        return out

    with hive:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(hive, index)
            except OSError:
                break
            index += 1

            match = re.search(r"VID&(?:0001)?([0-9A-Fa-f]{4})_PID&([0-9A-Fa-f]{4})",
                              sub)
            if not match or match.group(1).upper() != APPLE_VID:
                continue
            pid = int(match.group(2), 16)

            try:
                node = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            with node:
                inner = 0
                while True:
                    try:
                        instance = winreg.EnumKey(node, inner)
                    except OSError:
                        break
                    inner += 1
                    try:
                        with winreg.OpenKey(node, instance) as leaf:
                            friendly = winreg.QueryValueEx(
                                leaf, "FriendlyName")[0]
                    except OSError:
                        continue
                    # the entry is a driver resource string with the device
                    # name in brackets: "...;%1 A2DP SNK%0\n;(My AirPods)"
                    named = re.findall(r"\(([^()]+)\)", friendly)
                    name = named[-1] if named else friendly
                    out.append((pid, name.strip()))
    return out


def _normalise(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _user_overrides():
    """Optional calibration file, so a user can correct their own unit."""
    path = os.path.join(os.environ.get("APPDATA", ""), "AirPodsSoundLevels",
                        "calibration.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def identify(output_name):
    """Match the current audio output against the paired Bluetooth devices."""
    device = Device(output_name or "")
    if not output_name:
        return device

    target = _normalise(output_name)
    best = None
    for pid, name in _registry_devices():
        key = _normalise(name)
        if not key:
            continue
        if key in target or target in key:
            # prefer the longest match, so "AirPods Pro" beats "AirPods"
            if best is None or len(key) > len(best[1]):
                best = (pid, key, name)

    overrides = _user_overrides()

    if best is not None:
        pid = best[0]
        device.pid = pid
        device.model = MODELS.get(pid) or PROVISIONAL.get(pid)
        if pid in REFERENCES:
            device.reference, device.confidence = REFERENCES[pid]
        elif device.model:
            device.reference, device.confidence = _family(device.model)
        else:
            # a model newer than the table: fall back on the name the user
            # gave the device, which usually still says "AirPods Pro"
            device.reference, device.confidence = _family(output_name)

    key = "0x{:04X}".format(device.pid) if device.pid else None
    if key and key in overrides:
        device.reference = float(overrides[key])
        device.confidence = "user"
    elif "default" in overrides:
        device.reference = float(overrides["default"])
        device.confidence = "user"

    return device


def _family(text):
    for needle, value in FAMILIES:
        if needle.lower() in (text or "").lower():
            return value, "estimated"
    return DEFAULT_REFERENCE, "default"
