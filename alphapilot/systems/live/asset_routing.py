"""Asset-route capability contracts independent of any broker SDK.

The first executable profile is deliberately narrow: long-only A-share cash
equities/funds. Futures capability can be advertised by a native SDK, but it
cannot become routable until a dedicated position/risk/session model is
registered here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from alphapilot.systems.live.types import Contract, Product


@dataclass(frozen=True)
class AssetRouteProfile:
    name: str
    asset_classes: tuple[str, ...]
    session_profile: str
    position_model: str
    offset_requirement: str
    notional_model: str

    def accepts(self, contract: Contract) -> bool:
        return contract.product.value in self.asset_classes


class AssetRouteCapability(Protocol):
    @property
    def profile(self) -> AssetRouteProfile: ...

    def validate_contract(self, contract: Contract) -> None: ...


class AShareCashRouteCapability:
    """Executable AlphaPilot v1 route model."""

    profile = AssetRouteProfile(
        name="ashare_cash",
        asset_classes=(Product.EQUITY.value, Product.FUND.value),
        session_profile="ashare_cash",
        position_model="long_only_t1",
        offset_requirement="none",
        notional_model="cash",
    )

    def validate_contract(self, contract: Contract) -> None:
        if contract.product not in {Product.EQUITY, Product.FUND}:
            raise ValueError(
                f"{contract.product.value} is SDK-discoverable but not routable by "
                "the A-share cash position/risk model"
            )


ASHARE_CASH_ROUTE = AShareCashRouteCapability()


def route_capability_for(contract: Contract) -> AssetRouteCapability:
    """Resolve an executable route profile, failing closed for future models."""

    ASHARE_CASH_ROUTE.validate_contract(contract)
    return ASHARE_CASH_ROUTE
