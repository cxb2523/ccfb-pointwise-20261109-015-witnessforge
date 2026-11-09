"""Encode and decode fixed-length EBCDIC records (DISPLAY text, zoned, packed and binary)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List

from .copybook import Field, Layout

CODEPAGE = "cp037"
EBCDIC_SPACE = b"\x40"
_POSITIVE = {0xA, 0xC, 0xE, 0xF}
_NEGATIVE = {0xB, 0xD}


class CodecError(ValueError):
    """Raised when a value cannot be encoded, or bytes cannot be decoded, for a field."""


def _quantum(field: Field) -> Decimal:
    return Decimal(1).scaleb(-field.scale)


def _digit_string(field: Field, value: Any) -> str:
    number = Decimal(value)
    if number < 0 and not field.signed:
        raise CodecError(f"{field.name}: negative value {number} for an unsigned field")
    scaled = abs(number) * (Decimal(10) ** field.scale)
    if scaled != scaled.to_integral_value():
        raise CodecError(f"{field.name}: {number} has more than {field.scale} decimal places")
    digits = str(int(scaled))
    if len(digits) > field.total_digits:
        raise CodecError(f"{field.name}: {number} does not fit {field.describe()}")
    return digits.zfill(field.total_digits)


def digits_of(field: Field, value: Any) -> str:
    """Zero-padded decimal digits for an unsigned, unscaled PIC 9 / COMP field.

    Used by mapping steps (such as COBOL date fields) that need the exact source
    digit width and padding rather than the numeric ``Decimal`` value, since a
    leading zero (e.g. year ``05``) is otherwise lost once the field is read as a
    plain integer.
    """
    if field.kind not in ("9", "B") or field.scale or field.signed:
        raise CodecError(f"{field.name}: digits_of needs an unsigned integer PIC 9 or COMP field")
    digits = str(int(Decimal(value))).zfill(field.int_digits)
    if len(digits) > field.int_digits:
        raise CodecError(f"{field.name}: {value} does not fit {field.int_digits} digits")
    return digits


def encode_item(field: Field, value: Any) -> bytes:
    """Encode one occurrence of a field."""
    if field.kind == "X":
        text = "" if value is None else str(value)
        if len(text) > field.item_length:
            raise CodecError(f"{field.name}: {text!r} is longer than {field.item_length}")
        try:
            return text.ljust(field.item_length).encode(CODEPAGE)
        except UnicodeEncodeError as exc:
            raise CodecError(f"{field.name}: {text!r} has characters outside {CODEPAGE}") from exc

    if value is None:
        value = 0
    negative = Decimal(value) < 0
    digits = _digit_string(field, value)
    if field.kind == "9":
        out = bytearray(0xF0 | int(d) for d in digits)
        if field.signed:
            out[-1] = (0xD0 if negative else 0xC0) | int(digits[-1])
        return bytes(out)
    if field.kind == "B":
        number = -int(digits) if negative else int(digits)
        order = "little" if field.little_endian else "big"
        try:
            return number.to_bytes(field.item_length, order, signed=field.signed)
        except OverflowError as exc:
            raise CodecError(f"{field.name}: {value} does not fit {field.item_length} bytes") from exc

    sign =0xD if negative else (0xC if field.signed else 0xF)
    nibbles = [int(d) for d in digits] + [sign]
    if len(nibbles) % 2:
        nibbles.insert(0, 0)
    return bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles), 2))


def decode_item(field: Field, data: bytes) -> Any:
    """Decode one occurrence of a field. Numeric values come back as Decimal."""
    if field.kind == "X":
        return data.decode(CODEPAGE)

    negative = False
    if field.kind == "B":
        order = "little" if field.little_endian else "big"
        number = int.from_bytes(data, order, signed=field.signed)
        value = Decimal(number).scaleb(-field.scale).quantize(_quantum(field))
        return value if number else abs(value)
    if field.kind == "9":
        digits = []
        for index, byte in enumerate(data):
            zone, digit = byte >> 4, byte & 0xF
            if digit > 9:
                raise CodecError(f"{field.name}: invalid zoned digit 0x{byte:02X}")
            if index == len(data) - 1 and field.signed:
                if zone in _NEGATIVE:
                    negative = True
                elif zone not in _POSITIVE:
                    raise CodecError(f"{field.name}: invalid sign zone 0x{byte:02X}")
            elif zone != 0xF:
                raise CodecError(f"{field.name}: invalid zoned byte 0x{byte:02X}")
            digits.append(digit)
    else:
        nibbles: List[int] = []
        for byte in data:
            nibbles.extend((byte >> 4, byte & 0xF))
        sign = nibbles.pop()
        if any(n > 9 for n in nibbles):
            raise CodecError(f"{field.name}: invalid packed digit in {data.hex().upper()}")
        if sign in _NEGATIVE:
            negative = True
        elif sign not in _POSITIVE:
            raise CodecError(f"{field.name}: invalid packed sign nibble 0x{sign:X}")
        digits = nibbles

    number = int("".join(str(d) for d in digits) or "0")
    value = Decimal(number).scaleb(-field.scale).quantize(_quantum(field))
    return -value if negative and number else value


def _items(field: Field, value: Any) -> List[Any]:
    if field.occurs == 1:
        return [value]
    values = list(value or [])
    if len(values) > field.occurs:
        raise CodecError(f"{field.name}: {len(values)} items for OCCURS {field.occurs}")
    return values + [None] * (field.occurs - len(values))


def encode_record(layout: Layout, record: Dict[str, Any]) -> bytes:
    """Encode a record dict into one fixed-length EBCDIC record."""
    unknown = set(record) - set(layout.names())
    if unknown:
        raise CodecError(f"Unknown fields: {sorted(unknown)}")
    buffer = bytearray(EBCDIC_SPACE * layout.record_length)
    for field in layout.fields:
        for index, item in enumerate(_items(field, record.get(field.name))):
            start = field.offset + index * field.item_length
            buffer[start : start + field.item_length] = encode_item(field, item)
    return bytes(buffer)


def decode_record(layout: Layout, data: bytes) -> Dict[str, Any]:
    """Decode one fixed-length record into a dict of field name -> value (or list)."""
    if len(data) != layout.record_length:
        raise CodecError(f"Record is {len(data)} bytes, layout expects {layout.record_length}")
    record: Dict[str, Any] = {}
    for field in layout.fields:
        if field.offset < 0 or field.offset + field.length > len(data):
            raise CodecError(f"{field.name}: field extends outside the record")
        items = [
            decode_item(field, data[start : start + field.item_length])
            for start in range(field.offset, field.offset + field.length, field.item_length)
        ]
        record[field.name] = items if field.occurs > 1 else items[0]
    return record


def split_records(layout: Layout, data: bytes) -> List[bytes]:
    """Split a fixed-block (RECFM=F) file into records."""
    size = layout.record_length
    if len(data) % size:
        raise CodecError(f"File size {len(data)} is not a multiple of record length {size}")
    return [data[i : i + size] for i in range(0, len(data), size)]
