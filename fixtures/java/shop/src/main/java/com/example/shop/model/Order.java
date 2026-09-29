package com.example.shop.model;

import java.util.ArrayList;
import java.util.List;

/**
 * An order made of lines.
 */
@Audited
public class Order implements Priced {

    private final List<Line> lines = new ArrayList<>();
    private int discount, shipping = 5;
    Status status = Status.OPEN;

    public Order() {
        this(0);
    }

    public Order(int discount) {
        this.discount = discount;
    }

    /** Add one item. */
    public Order add(Item item) {
        return add(item, 1);
    }

    public Order add(Item item, int quantity) {
        lines.add(new Line(item, quantity));
        return this;
    }

    public Order addAll(Item... items) {
        for (Item item : items) {
            add(item);
        }
        return this;
    }

    public <T extends Comparable<T>> T largest(List<T> values) {
        return values.stream().max(Comparable::compareTo).orElseThrow();
    }

    @Override
    public double total() {
        double sum = 0;
        for (Line line : lines) {
            sum += line.subtotal();
        }
        return sum - discount + shipping;
    }

    @Deprecated
    void validate() {
        if (lines.isEmpty()) {
            throw new IllegalStateException("empty order");
        }
    }

    /** A single order line. */
    public static class Line {
        private final Item item;
        private final int quantity;

        Line(Item item, int quantity) {
            this.item = item;
            this.quantity = quantity;
        }

        double subtotal() {
            return item.price() * quantity;
        }
    }

    class Tracker {
        void track() {
            validate();
        }
    }

    enum Channel { WEB, STORE }
}
