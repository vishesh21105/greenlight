from tinyshop.checkout import checkout
from tinyshop.models import Item


def test_checkout_total():
    cart = [Item("pen", 10, 2), Item("book", 250, 1)]
    assert checkout(cart) == 319


def test_checkout_with_discount():
    cart = [Item("book", 1000, 1)]
    assert checkout(cart, discount_percent=10) == 1062
