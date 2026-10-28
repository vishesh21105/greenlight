from dataclasses import dataclass


@dataclass
class Item:
    name: str
    price: int      # in rupees
    qty: int = 1
