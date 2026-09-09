"""Display-only color matches; neither spool identity nor material validation.

Exact opaque single-color PLA Basic names from Bambu Studio's catalog:
https://github.com/bambulab/BambuStudio/blob/master/resources/profiles/BBL/filament/filaments_color_codes.json
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from .backend import AmsTrayReading
from .config import TrayColorLabel

ColorSource = Literal["bambu_color_match", "operator_declared", "generic", "unknown"]

PLA_BASIC = {
    "FF6A13": "Orange", "FF9016": "Pumpkin Orange", "5B6579": "Blue Gray",
    "0056B8": "Cobalt Blue", "00B1B7": "Turquoise", "0086D6": "Cyan",
    "0A2989": "Blue", "8E9089": "Gray", "A6A9AA": "Silver",
    "D1D3D5": "Light Gray", "545454": "Dark Gray", "3F8E43": "Mistletoe Green",
    "BECF00": "Bright Green", "00AE42": "Bambu Green", "000000": "Black",
    "9D432C": "Brown", "6F5034": "Cocoa Brown", "F7E6DE": "Beige",
    "F55A74": "Pink", "482960": "Indigo Purple", "5E43B7": "Purple",
    "EC008C": "Magenta", "C12E1F": "Red", "9D2235": "Maroon Red",
    "F5547C": "Hot Pink", "FFFFFF": "Jade White", "F4EE2A": "Yellow",
    "FEC600": "Sunflower Yellow", "847D48": "Bronze", "E4BD68": "Gold",
}


def tray_color_label(
    tray: AmsTrayReading, declarations: Sequence[TrayColorLabel] = (),
) -> tuple[str, ColorSource]:
    code = (tray.tray_color or "").lstrip("#").upper()
    material = (tray.tray_type or "").strip().upper()
    for declaration in declarations:
        if (
            (tray.ams_id, tray.tray_id) == (declaration.ams_id, declaration.tray_id)
            and material == declaration.material.strip().upper()
            and code == declaration.reported_color.lstrip("#").upper()
        ):
            return declaration.name, "operator_declared"
    if not re.fullmatch(r"[0-9A-F]{6}(FF)?", code):
        return "Unknown color", "unknown"
    rgb = code[:6]
    if material == "PLA" and rgb in PLA_BASIC:
        return PLA_BASIC[rgb], "bambu_color_match"
    if material in {"PETG", "PC"} and rgb == "FFFFFF":
        return "White", "bambu_color_match"
    generic = {"FFFFFF": "White", "000000": "Black", "F72323": "Red", "FF0000": "Red"}
    return generic.get(rgb, "Custom color"), "generic"
