package com.example.shop.model;

public record Item(String sku, double price) {
    public Item {
        if (price < 0) {
            throw new IllegalArgumentException("negative price");
        }
    }
}
