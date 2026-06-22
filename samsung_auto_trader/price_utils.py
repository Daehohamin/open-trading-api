def get_tick_size(price: int) -> int:
    if price <= 0:
        raise ValueError("price must be positive")
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def normalize_order_price(price: int, side: str) -> int:
    tick_size = get_tick_size(price)
    if side == "buy":
        return price - (price % tick_size)
    if side == "sell":
        remainder = price % tick_size
        if remainder == 0:
            return price
        return price + tick_size - remainder
    raise ValueError("side must be 'buy' or 'sell'")
