"""A custom strategy for `python -m vaalstreet backtest examples/cheap_vs_divine.py:CheapVsDivine`.

Buy liquid items a little under fair value in the Chaos market and offer them at a fixed
markup in the Divine market. Everything `ctx` exposes is known at the current hour.
"""

import numpy as np


class CheapVsDivine:
    def __init__(self, discount: float = 0.04, markup: float = 0.08, top: int = 30, per_item: float = 0.03):
        self.discount, self.markup, self.top, self.per_item = discount, markup, top, per_item

    def step(self, ctx):
        if ctx.t < 48:  # let two days of history build up first
            return
        traded = np.nansum(ctx.history("value", 48), axis=0)
        liquid = traded >= np.sort(traded)[-min(self.top, len(traded))]
        fair = ctx.fair
        units = np.where(liquid, self.per_item * ctx.bankroll / fair, 0.0)
        ctx.quote_buys("r", np.where(liquid, fair * (1 - self.discount), np.nan), units)  # "r" = Chaos market
        ctx.quote_sells("b", ctx.average_cost * (1 + self.markup), ctx.position)  # "b" = Divine market
