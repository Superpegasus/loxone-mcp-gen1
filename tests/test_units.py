"""Unit tests without a Loxone project: python tests/test_units.py"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import icons  # noqa: E402
from analyze import BAD_TITLE  # noqa: E402
from backup import decompress_loxcc  # noqa: E402
from loxproject import Project, ProjectError  # noqa: E402
import plan  # noqa: E402


def loxcc(block: bytes, usize: int) -> bytes:
    return struct.pack("<LLLL", 0xAABBCCEE, len(block), usize, 0) + block


def test_loxcc_literals_and_match():
    # token 0x34: 3 literals "abc", match length 4+4=8, offset 3 -> "abc" + "abcabcab", then 2 literals "XY"
    block = bytes([0x34]) + b"abc" + bytes([3, 0]) + bytes([0x20]) + b"XY"
    assert decompress_loxcc(loxcc(block, 13)) == b"abcabcabcabXY"


def test_loxcc_bad_header():
    try:
        decompress_loxcc(b"\x00" * 20)
    except ProjectError:
        return
    raise AssertionError("expected ProjectError")


def test_color_bgr():
    assert icons.color("#ff0000") == 255           # red -> R
    assert icons.color("#0000ff") == 255 * 65536   # blue -> B<<16
    assert icons.color("#70ff8d") == 0x70 + 0xFF * 256 + 0x8D * 65536


def test_bad_title():
    assert BAD_TITLE.search("Pos 70%") and BAD_TITLE.search("a<b")
    assert not BAD_TITLE.search("Fenster offen (Anzahl > 0)")


def test_plan_aliases():
    assert plan.ALIASES["connect"] == "verbinden" and plan.ALIASES["disconnect"] == "trennen"
    assert plan._first_hash({"angelegt": "X [And] #21a0b93a-0058-5f07"}) == "#21a0b93a-0058-5f07"


def test_parser_digit_attribute(tmp=Path(__file__).with_name("_tmp.Loxone")):
    xml = ('<?xml version="1.0" encoding="utf-8"?><ControlList><C Type="Document" U="1" Title="t" 12hTF="false">'
           '</C></ControlList>')
    tmp.write_text(xml, encoding="utf-8")
    try:
        Project(tmp)  # must not raise although 12hTF is not valid XML
    finally:
        tmp.unlink()


if __name__ == "__main__":
    tests = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
