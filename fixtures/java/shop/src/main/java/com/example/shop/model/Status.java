package com.example.shop.model;

public enum Status {
    OPEN("open"),
    PAID("paid") {
        @Override
        String label() {
            return "paid!";
        }
    },
    SHIPPED("shipped");

    private final String code;

    Status(String code) {
        this.code = code;
    }

    String label() {
        return code;
    }
}
