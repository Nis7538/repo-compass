package com.example.shop.service;

import static com.example.shop.util.Money.round;

import com.example.shop.model.*;
import com.example.shop.model.Order.Line;
import java.util.List;
import java.util.function.Supplier;

public class OrderService extends BaseService {

    private final Supplier<Order> factory = Order::new;

    public double checkout(List<Item> items) {
        Order order = factory.get();
        for (Item item : items) {
            order.add(item);
        }
        order.add(items.get(0), 2);
        super.validate();
        this.audit(order);
        Runnable r = () -> log("checked out");
        r.run();
        new Thread(new Runnable() {
            @Override
            public void run() {
                helper();
            }
        }).start();
        return round(order.total());
    }

    public List<Double> totals(List<Order> orders) {
        return orders.stream().map(Order::total).toList();
    }

    Line firstLine(Item item) {
        return new Order.Line(item, 1);
    }

    private void audit(Order order) {
        log(String.valueOf(order.total()));
    }

    private void helper() {
        validate();
    }
}
