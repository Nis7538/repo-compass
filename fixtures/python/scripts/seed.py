"""A script outside any package, using an aliased import."""

from inventory.models import load
from inventory.models import store as persist

persist(load("abc"))
