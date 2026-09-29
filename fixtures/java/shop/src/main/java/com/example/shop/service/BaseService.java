package com.example.shop.service;

public abstract class BaseService {
    protected void validate() {}

    protected void log(String message) {
        System.out.println(message);
    }
}
