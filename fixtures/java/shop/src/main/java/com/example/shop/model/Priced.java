package com.example.shop.model;

public interface Priced {
    double total();

    default boolean isFree() {
        return total() == 0;
    }
}
