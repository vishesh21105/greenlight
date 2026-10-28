import pytest

from tinyshop.models import Item
from tinyshop.pricing import add_gst, apply_discount, compute_total


def test_compute_total():
    assert compute_total([Item("pen", 10, 2), Item("book", 250)]) == 270


def test_compute_total_empty():
    assert compute_total([]) == 0


def test_apply_discount():
    assert apply_discount(1000, 10) == 900


def test_apply_discount_rejects_bad_percent():
    with pytest.raises(ValueError):
        apply_discount(100, 120)


def test_add_gst():
    assert add_gst(100) == 118
