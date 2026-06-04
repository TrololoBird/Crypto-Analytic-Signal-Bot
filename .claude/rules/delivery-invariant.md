# Delivery invariant (always)

Signal delivery must follow this order with no shortcuts:

1. `validate_signal_contract`
2. `hard_confluence_gate` (minimum 3 of 5 confluence factors)
3. `delivery.deliver`

No auto-trading. No private Binance API endpoints. Telegram is manual signals only.

When editing delivery code, run delivery tests before claiming done.
