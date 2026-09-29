package com.example.shop.other;

/** Unrelated class whose method names collide with Order's (resolution distractor). */
public class Report {
    public double total() {
        return 0;
    }

    public void add(Object row, int count) {}
}
