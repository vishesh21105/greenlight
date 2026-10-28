from tinyshop.cart import add_item, remove_item
from tinyshop.models import Item


def test_add_new_item():
    assert len(add_item([], Item("pen", 10))) == 1


def test_add_existing_item_increases_qty():
    cart = add_item([Item("pen", 10, 1)], Item("pen", 10, 2))
    assert cart[0].qty == 3


def test_remove_item():
    assert remove_item([Item("pen", 10)], "pen") == []
