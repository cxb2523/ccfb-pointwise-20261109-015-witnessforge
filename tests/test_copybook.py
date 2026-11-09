import pytest

from witnessforge.copybook import CopybookError, parse_copybook


def test_example_layout(layout):
    assert layout.record_length == 82
    amt = layout.field("BILLED-AMT")
    assert (amt.kind, amt.int_digits, amt.scale, amt.signed) == ("P", 7, 2, True)
    assert amt.item_length == 5
    diag = layout.field("DIAG-CODE")
    assert diag.occurs == 3 and diag.length == 21
    assert layout.field("SERVICE-DATE").offset == 24


def test_repeat_and_explicit_pictures():
    layout = parse_copybook(
        """
        01 R.
           05 A PIC XXX.
           05 B PIC 9(3)V9(2).
           05 C PIC S999 COMP-3.
           05 FILLER PIC X(2).
        """
    )
    assert [f.name for f in layout.fields] == ["A", "B", "C", "FILLER-1"]
    assert layout.field("B").item_length == 5
    assert layout.field("C").item_length == 2
    assert layout.record_length == 12


def test_binary_fields():
    layout = parse_copybook("01 R.\n 05 A PIC S9(4) COMP.\n 05 B PIC 9(9) BINARY.\n 05 C PIC S9(15)V99 COMP-5.\n")
    assert [f.kind for f in layout.fields] == ["B", "B", "B"]
    assert [f.item_length for f in layout.fields] == [2, 4, 8]
    assert layout.record_length == 14


def test_sequence_area_and_comments_are_ignored():
    text = "000100* comment\n000200 01 R.\n000300    05 A PIC X(4).\n"
    assert parse_copybook(text).record_length == 4


@pytest.mark.parametrize(
    "line",
    [
        "05 A PIC X(4) REDEFINES B.",
        "05 A PIC S9(4) COMP-1.",
        "05 G OCCURS 2 TIMES.",
        "05 A PIC ZZ9.99.",
    ],
)
def test_unsupported_constructs_fail_loudly(line):
    with pytest.raises(CopybookError):
        parse_copybook(f"01 R.\n {line}\n")
