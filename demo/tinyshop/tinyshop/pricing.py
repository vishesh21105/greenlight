from tinyshop.models import Item

GST_RATE = 0.18


def compute_total(cart: list[Item]) -> int:
    """Sum of price x quantity for every item in the cart."""
    return sum(item.price * item.qty for item in cart)


def apply_discount(amount: int, percent: int) -> int:
    """Apply a percentage discount, rounding down to whole rupees."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return amount * (100 - percent) // 100


def add_gst(amount: int) -> int:
    return round(amount * (1 + GST_RATE))
