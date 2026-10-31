"""Discount calculations."""


def apply_discount(subtotal_cents: int, percent: int) -> int:
    """Return the amount due, in cents, after a percentage discount."""
    if percent < 0 or percent > 100:
        raise ValueError("percent must be between 0 and 100")
    return subtotal_cents - subtotal_cents // (100 // percent)
