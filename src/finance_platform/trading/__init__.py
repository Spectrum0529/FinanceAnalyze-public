"""USD cash simulation and paper-only trading boundary."""
from .accounting import (CashBook, FeeSchedule, LedgerEvent, PortfolioAllocator,
                         TradingError, decimal, money, execution_price, executable_quantity)

__all__ = ["CashBook", "FeeSchedule", "LedgerEvent", "PortfolioAllocator", "TradingError",
           "decimal", "money", "execution_price", "executable_quantity"]


def initialize(runtime):
    from .service import TradingService
    runtime.trading = TradingService(runtime.database, authorize=runtime.require_owner)
