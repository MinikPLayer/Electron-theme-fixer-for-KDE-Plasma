#!/bin/bash

MIN_MS=250
MAX_MS=1000

function random_sleep() {
    local sleep_ms=0.$((MIN_MS + RANDOM % (MAX_MS-MIN_MS)))
    sleep $sleep_ms
}

while true; do
    plasma-apply-colorscheme BreezeDark
    random_sleep
    plasma-apply-colorscheme BreezeLight
    random_sleep
done