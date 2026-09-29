"""Domain models for the inventory fixture."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .helpers import retry

if TYPE_CHECKING:
    from .services import StockService


class Base:
    """Common base for stored objects."""

    def __init__(self, key):
        self.key = key

    def save(self):
        """Persist the object."""
        return store(self)


class Meta(type):
    pass


@dataclass
class Item(Base, metaclass=Meta):
    """A stock-keeping unit."""

    sku: str
    quantity: int = 0

    @property
    def label(self):
        return self.sku.upper()

    @label.setter
    def label(self, value):
        self.sku = value.lower()

    @staticmethod
    def parse(raw):
        return Item(raw.strip())

    @classmethod
    def empty(cls):
        return cls("")

    @retry(3)
    def save(self):
        super().save()
        return self.validate()

    def validate(self):
        def check(value):
            return value >= 0

        return check(self.quantity)

    class History:
        def record(self, event):
            return event

    async def refresh(self, service: "StockService"):
        await service.reload(self)


def store(obj):
    return obj


def load(sku, factory=Item.parse):
    return factory(sku)


try:
    from fastjson import dumps
except ImportError:

    def dumps(obj):
        return str(obj)
