package com.example.shop.util;

public final class Money {
    private Money() {}

    public static double round(double value) {
        return round(value, 2);
    }

    public static double round(double value, int places) {
        double scale = Math.pow(10, places);
        return Math.round(value * scale) / scale;
    }
}
