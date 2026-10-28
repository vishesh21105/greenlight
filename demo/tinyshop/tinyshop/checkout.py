from tinyshop.models import Item
from tinyshop.pricing import add_gst, apply_discount, compute_total


def checkout(cart: list[Item], discount_percent: int = 0) -> int:
    """Final amount to pay: total, minus discount, plus GST."""
    subtotal = compute_total(cart)
    discounted = apply_discount(subtotal, discount_percent)
    return add_gst(discounted)
