#!/usr/bin/env python3
"""
Strip EXIF and other metadata segments from generated images.

Scope, stated plainly because the badge in the UI depends on it being true:

  REMOVED   JPEG APP1..APP15 (EXIF, XMP, Photoshop/IPTC), COM comments;
            PNG tEXt / zTXt / iTXt / eXIf / tIME chunks.
            That covers camera data, timestamps, software/producer tags, local
            file paths, and any XMP a generator wrote in.

  KEPT      JPEG APP0 (JFIF) and the compressed image data; PNG critical chunks
            plus colour-management ones (iCCP, sRGB, gAMA, cHRM). Dropping colour
            management changes how the ad renders — that is a visual regression,
            not privacy hygiene.

  NOT DONE  Nothing here defeats AI-generation detection. Platforms classify AI
            imagery from pixel analysis, not tags, and C2PA content credentials are
            deliberately left intact. This is publishing hygiene, not concealment;
            label it as such in any UI so nobody relies on it for the wrong thing.

Standard library only.
"""

from __future__ import annotations

import struct

# JPEG markers that carry no image data and can be dropped whole.
_JPEG_DROP = {0xFE}                          # COM
_JPEG_DROP |= set(range(0xE1, 0xF0))         # APP1..APP15  (APP0/JFIF is kept)

_JPEG_NAMES = {0xE1: "EXIF/XMP (APP1)", 0xE2: "ICC-ish (APP2)", 0xED: "IPTC (APP13)",
               0xEE: "Adobe (APP14)", 0xFE: "comment"}

# PNG ancillary chunks that are pure metadata.
_PNG_DROP = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"}


def _strip_jpeg(data: bytes):
    if data[:2] != b"\xff\xd8":
        return data, []
    out = bytearray(b"\xff\xd8")
    removed, i, n = [], 2, len(data)
    while i < n - 1:
        if data[i] != 0xFF:
            # Desynchronised — bail out and keep the remainder verbatim rather
            # than risk corrupting the file.
            out += data[i:]
            break
        marker = data[i + 1]
        if marker == 0xD8:                       # stray SOI
            i += 2
            continue
        if marker == 0xDA:                       # SOS: entropy-coded data follows
            out += data[i:]
            break
        if 0xD0 <= marker <= 0xD9:               # standalone markers
            out += data[i:i + 2]
            i += 2
            continue
        if i + 4 > n:
            out += data[i:]
            break
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if seg_len < 2 or i + 2 + seg_len > n:
            out += data[i:]
            break
        if marker in _JPEG_DROP:
            removed.append(_JPEG_NAMES.get(marker, f"APP{marker - 0xE0}"))
        else:
            out += data[i:i + 2 + seg_len]
        i += 2 + seg_len
    return bytes(out), removed


def _strip_png(data: bytes):
    sig = b"\x89PNG\r\n\x1a\n"
    if data[:8] != sig:
        return data, []
    out = bytearray(sig)
    removed, i, n = [], 8, len(data)
    while i + 8 <= n:
        length = struct.unpack(">I", data[i:i + 4])[0]
        ctype = data[i + 4:i + 8]
        end = i + 12 + length                    # len + type + data + crc
        if end > n:
            out += data[i:]
            break
        if ctype in _PNG_DROP:
            removed.append(ctype.decode("ascii", "replace"))
        else:
            out += data[i:end]
        i = end
        if ctype == b"IEND":
            break
    return bytes(out), removed


def strip(data: bytes):
    """Return (clean_bytes, removed_labels). Unknown formats pass through
    unchanged with an empty list — never silently claim a strip that didn't
    happen."""
    if not data:
        return data, []
    if data[:2] == b"\xff\xd8":
        return _strip_jpeg(data)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _strip_png(data)
    return data, []


def describe(removed, before=0, after=0):
    """One honest line for a UI badge."""
    if not removed:
        return "No metadata segments found — nothing to strip"
    saved = f", {before - after:,} bytes" if before and after and before > after else ""
    return f"EXIF stripped: {', '.join(sorted(set(removed)))}{saved}"


if __name__ == "__main__":
    import sys
    for path in sys.argv[1:]:
        raw = open(path, "rb").read()
        clean, removed = strip(raw)
        print(f"{path}: {describe(removed, len(raw), len(clean))}")
