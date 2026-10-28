"""Tiny sample used to illustrate the Luciole graph format."""

TAX_RATE = 0.2


def with_tax(amount):
    total = amount * (1 + TAX_RATE)
    return round(total, 2)


class Cart:
    def __init__(self):
        self.subtotal = 0

    def add(self, price):
        self.subtotal += price

    def total(self):
        return with_tax(self.subtotal)


def main():
    cart = Cart()
    cart.add(10)
    cart.add(5)
    due = cart.total()
    print(f"Total: {due}")


if __name__ == "__main__":
    main()
