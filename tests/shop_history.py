"""One known story in git, for diff_impact and hotspots tests. Written from scratch.

A small Java shop (under shop/) and a Python inventory package (under inventory/).

main, one commit a week from 2026-01-05 (merges are skipped by hotspots):
  c1 01-05  everything, Python sync code in inventory/sync_job.py
  c2 01-12  Order.java edited
  c3 01-19  Order.java edited; sync_job.py renamed to sync.py
  c4 01-26  (branch tweak) Order.java edited
  c5 02-02  CartService.java edited
  c6 02-09  merge of tweak
  c7 02-16  Order.java edited
So Order.java has 5 non-merge commits, sync.py 2 (one before its rename).

feature, from main, 2026-03-02, one commit:
  - removes Order.addIfAbsent, still called from ImportJob (untouched) and OrderTest
  - changes Order.add(Item) to add(Item, int); updates CartService, leaves ImportJob
  - changes the body of Order.total (called from SalesReport, untouched)
  - adds Order.clear
  - re-indents Order.size (must not be reported)
  - edits the test OrderTest.totals
  - removes inventory.stock.release, still imported and called by inventory/sync.py
  - edits README.md (not code)
"""

from tests.gitrepo import ScriptedRepo

JAVA = "shop/src/main/java/com/example/shop/"
ORDER = JAVA + "model/Order.java"

ORDER_MAIN = """package com.example.shop.model;

import java.util.ArrayList;
import java.util.List;

public class Order {
    private final List<Item> items = new ArrayList<>();

    public Order add(Item item) {
        items.add(item);
        return this;
    }

    public boolean addIfAbsent(Item item) {
        if (items.contains(item)) {
            return false;
        }
        add(item);
        return true;
    }

    public double total() {
        double sum = 0;
        for (Item item : items) {
            sum += item.price();
        }
        return sum;
    }

    public int size() {
        return items.size();
    }
    // revision REV
}
"""

ORDER_FEATURE = """package com.example.shop.model;

import java.util.ArrayList;
import java.util.List;

public class Order {
    private final List<Item> items = new ArrayList<>();

    public Order add(Item item, int qty) {
        for (int i = 0; i < qty; i++) {
            items.add(item);
        }
        return this;
    }

    public double total() {
        double sum = 0;
        for (Item item : items) {
            sum += item.price() * 1.2;
        }
        return sum;
    }

    public void clear() {
        items.clear();
    }

      public int size() {
          return items.size();
      }
    // revision 7
}
"""

ITEM = """package com.example.shop.model;

public record Item(String sku, double price) {
}
"""

CART = """package com.example.shop.service;

import com.example.shop.model.Item;
import com.example.shop.model.Order;

public class CartService {
    private final Order order = new Order();

    public void addToCart(Item item) {
        order.add(item);
    }

    public double checkout() {
        return order.total();
    }
}
"""

IMPORT_JOB = """package com.example.shop.job;

import com.example.shop.model.Item;
import com.example.shop.model.Order;

public class ImportJob {
    public Order run(Item first, Item second) {
        Order order = new Order();
        order.add(first);
        order.addIfAbsent(second);
        return order;
    }
}
"""

SALES_REPORT = """package com.example.shop.report;

import com.example.shop.model.Order;

public class SalesReport {
    public double sum(Order order) {
        return order.total();
    }
}
"""

ORDER_TEST = """package com.example.shop.model;

public class OrderTest {
    public void addsOnce() {
        Order order = new Order();
        order.addIfAbsent(new Item("a", 1.0));
    }

    public void totals() {
        Order order = new Order();
        assert order.total() == 0;
    }
}
"""

STOCK = '''"""Stock levels."""

_levels = {}


def reserve(sku, qty):
    _levels[sku] = _levels.get(sku, 0) - qty
    return _levels[sku]


def release(sku, qty):
    _levels[sku] = _levels.get(sku, 0) + qty
    return _levels[sku]
'''

SYNC = '''"""Nightly stock sync."""

from inventory.stock import release, reserve


def sync(orders):
    for sku, qty in orders:
        reserve(sku, qty)
        release(sku, qty)
'''


def _order(revision: int) -> str:
    return ORDER_MAIN.replace("REV", str(revision))


def shop_history(repo: ScriptedRepo) -> ScriptedRepo:
    """Build main and feature as described above; leaves feature checked out."""
    repo.write("README.md", "# shop\n")
    repo.write(ORDER, _order(1))
    repo.write(JAVA + "model/Item.java", ITEM)
    repo.write(JAVA + "service/CartService.java", CART)
    repo.write(JAVA + "job/ImportJob.java", IMPORT_JOB)
    repo.write(JAVA + "report/SalesReport.java", SALES_REPORT)
    repo.write("shop/src/test/java/com/example/shop/model/OrderTest.java", ORDER_TEST)
    repo.write("inventory/__init__.py", "")
    repo.write("inventory/stock.py", STOCK)
    repo.write("inventory/sync_job.py", SYNC)
    repo.commit("c1 initial", "2026-01-05T10:00:00+00:00")
    repo.write(ORDER, _order(2))
    repo.commit("c2 order", "2026-01-12T10:00:00+00:00")
    repo.write(ORDER, _order(3))
    repo.rename("inventory/sync_job.py", "inventory/sync.py")
    repo.commit("c3 order, rename sync", "2026-01-19T10:00:00+00:00")
    repo.branch("tweak")
    repo.checkout("tweak")
    repo.write(ORDER, _order(4))
    repo.commit("c4 order", "2026-01-26T10:00:00+00:00")
    repo.checkout("main")
    repo.write(JAVA + "service/CartService.java", CART.replace("new Order()", "new Order() "))
    repo.commit("c5 cart", "2026-02-02T10:00:00+00:00")
    repo.merge("tweak", "c6 merge tweak", "2026-02-09T10:00:00+00:00")
    repo.write(ORDER, _order(7))
    repo.commit("c7 order", "2026-02-16T10:00:00+00:00")

    repo.branch("feature")
    repo.checkout("feature")
    repo.write("README.md", "# shop\n\nNow with quantities.\n")
    repo.write(ORDER, ORDER_FEATURE)
    repo.write(
        JAVA + "service/CartService.java",
        CART.replace("new Order()", "new Order() ").replace(
            "order.add(item)", "order.add(item, 1)"
        ),
    )
    repo.write(
        "shop/src/test/java/com/example/shop/model/OrderTest.java",
        ORDER_TEST.replace("== 0;", "== 0.0;"),
    )
    repo.write("inventory/stock.py", STOCK.split("\n\ndef release")[0] + "\n")
    repo.commit("c8 quantities", "2026-03-02T10:00:00+00:00")
    return repo
