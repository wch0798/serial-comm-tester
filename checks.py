"""
검사값(체크섬 / CRC) 계산

보낼 데이터 끝에 검사값을 자동으로 붙일 때 쓴다.
"""

import struct
import zlib

# (키, 화면 표시)
CHECKS = [
    ("none", "없음"),
    ("crc16_modbus", "CRC16 Modbus (하위바이트 먼저)"),
    ("crc16_ccitt", "CRC16 CCITT-FALSE"),
    ("crc16_xmodem", "CRC16 XMODEM"),
    ("crc8", "CRC8"),
    ("sum8", "체크섬 (합 1바이트)"),
    ("sum16", "체크섬 (합 2바이트)"),
    ("xor", "XOR (BCC)"),
    ("lrc", "LRC"),
    ("crc32", "CRC32 (하위바이트 먼저)"),
]
CHECK_KEYS = [k for k, _ in CHECKS]


def label_of(key):
    for k, lbl in CHECKS:
        if k == key:
            return lbl
    return CHECKS[0][1]


def key_of(label):
    for k, lbl in CHECKS:
        if lbl == label or k == label:
            return k
    return "none"


def crc16_modbus(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _crc16_ccitt(data, crc):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def crc8(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def compute(data, key):
    """검사값 바이트를 돌려준다."""
    data = bytes(data)
    if key == "crc16_modbus":
        return struct.pack("<H", crc16_modbus(data))
    if key == "crc16_ccitt":
        return struct.pack(">H", _crc16_ccitt(data, 0xFFFF))
    if key == "crc16_xmodem":
        return struct.pack(">H", _crc16_ccitt(data, 0x0000))
    if key == "crc8":
        return bytes([crc8(data)])
    if key == "sum8":
        return bytes([sum(data) & 0xFF])
    if key == "sum16":
        return struct.pack(">H", sum(data) & 0xFFFF)
    if key == "xor":
        x = 0
        for b in data:
            x ^= b
        return bytes([x])
    if key == "lrc":
        return bytes([(-sum(data)) & 0xFF])
    if key == "crc32":
        return struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)
    return b""


def append(data, key):
    return bytes(data) + compute(data, key) if key and key != "none" else bytes(data)
