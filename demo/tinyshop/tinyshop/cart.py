from tinyshop.models import Item


def add_item(cart: list[Item], item: Item) -> list[Item]:
    """Add an item; if it is already in the cart, increase its quantity."""
    for existing in cart:
        if existing.name == item.name:
            existing.qty += item.qty
            return cart
    cart.append(item)
    return cart


def remove_item(cart: list[Item], name: str) -> list[Item]:
    return [i for i in cart if i.name != name]
