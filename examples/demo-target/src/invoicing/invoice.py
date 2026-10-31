"""Invoice totals."""

from invoicing.discounts import apply_discount


def invoice_total(line_items_cents: list[int], discount_percent: int) -> int:
    """Return the total due for an invoice."""
    return apply_discount(sum(line_items_cents), discount_percent)
