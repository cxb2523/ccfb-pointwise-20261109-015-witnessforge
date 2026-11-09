from decimal import Decimal

import pytest

from witnessforge.codec import CodecError, decode_item, decode_record, encode_item, encode_record
from witnessforge.copybook import Field


def test_packed_decimal_known_bytes():
    field = Field("AMT", "P", 7, scale=2, signed=True)
    assert encode_item(field, Decimal("12345.67")).hex().upper() == "001234567C"
    assert encode_item(field, Decimal("-1.50")).hex().upper() == "000000150D"
    assert decode_item(field, bytes.fromhex("001234567C")) == Decimal("12345.67")


def test_zoned_signed_overpunch():
    field = Field("N", "9", 3, signed=True)
    assert encode_item(field, -123).hex().upper() == "F1F2D3"
    assert decode_item(field, bytes.fromhex("F1F2C3")) == Decimal("123")


def test_text_is_ebcdic_and_padded():
    field = Field("T", "X", 4)
    assert encode_item(field, "AB") == bytes.fromhex("C1C24040")
    assert decode_item(field, bytes.fromhex("C1C24040")) == "AB  "


def test_record_round_trip(layout):
    record = {
        "CLAIM-ID": "C1",
        "BILLED-AMT": Decimal("-10.25"),
        "UNITS": 7,
        "DIAG-CODE": ["E119", "I10"],
    }
    decoded = decode_record(layout, encode_record(layout, record))
    assert decoded["CLAIM-ID"].strip() == "C1"
    assert decoded["BILLED-AMT"] == Decimal("-10.25")
    assert [d.strip() for d in decoded["DIAG-CODE"]] == ["E119", "I10", ""]


@pytest.mark.parametrize(
    "field,value",
    [
        (Field("T", "X", 2), "TOO LONG"),
        (Field("N", "9", 2), 100),
        (Field("N", "9", 2), -1),
        (Field("N", "9", 2, scale=1), Decimal("1.23")),
    ],
)
def test_values_that_do_not_fit_are_rejected(field, value):
    with pytest.raises(CodecError):
        encode_item(field, value)


def test_invalid_bytes_are_rejected():
    with pytest.raises(CodecError):
        decode_item(Field("N", "9", 2), b"\x40\x40")
    with pytest.raises(CodecError):
        decode_item(Field("P", "P", 3), bytes.fromhex("12AB"))


def test_binary_big_endian_and_byte_order():
    field = Field("B", "B", 4, signed=True)
    assert encode_item(field, -2).hex().upper() == "FFFE"
    assert decode_item(field, bytes.fromhex("0102")) == Decimal(258)
    little = Field("B", "B", 4, signed=True, little_endian=True)
    assert decode_item(little, bytes.fromhex("0102")) == Decimal(513)
    cents = Field("C", "B", 7, scale=2, signed=True)
    assert decode_item(cents, encode_item(cents, Decimal("-1234.56"))) == Decimal("-1234.56")
