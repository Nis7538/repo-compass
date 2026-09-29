"""Services using the models: exercises import styles and call shapes."""

import inventory.models as m
from inventory import models

from . import utils
from .helpers import *  # noqa: F403
from .models import Base
from .models import Item as It


class StockService(Base):
    def __init__(self, repo):
        super().__init__("stock")
        self.repo = repo

    def add(self, sku, qty=1):
        item = It(sku)
        item.quantity += qty
        self.repo.save(item)
        item.save()
        return m.Item.parse(sku)

    def reload(self, item):
        return models.load(item.sku)

    def save(self):
        return utils.save(self)

    def report(self, items):
        names = [fmt(i.sku) for i in items]  # noqa: F405
        key = lambda i: i.label.lower()  # noqa: E731
        return sorted(names, key=key)


def build(default=m.Item.empty()):
    return StockService(default).add("x").validate()


if __name__ == "__main__":
    build()
