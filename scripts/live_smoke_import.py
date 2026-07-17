"""Live smoke: native gateways construct + compiled SDK bindings load. No server connection.

vn.py is no longer part of the live stack: XTP Pro and EMT run through
AlphaPilot-native gateways over the compiled vendor bindings. This smoke
proves, per broker: the binding imports, the
AlphaPilot gateway class resolves through the registry, and constructing it
instantiates the C++ API wrapper objects (load + link check). Exits non-zero on
any required failure so it can gate an image build. Which brokers are required
is controlled by LIVE_SMOKE_REQUIRE (comma list, default "xtp,emt"). ``tts``
also checks the independent trade-only and replay quote-only providers.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    require = {
        name.strip().lower()
        for name in os.getenv("LIVE_SMOKE_REQUIRE", "xtp,emt").split(",")
        if name.strip()
    }
    results: dict[str, str] = {}
    failed = False

    from alphapilot.systems.live.brokers.registry import (
        create_gateway,
        create_gateway_pair,
        create_quote_gateway,
    )
    from alphapilot.systems.live.gateway import BrokerGateway, QuoteGateway

    sdk_flags = {
        "xtp": "alphapilot_broker_xtp.gateway",
        "emt": "alphapilot_broker_emt.gateway",
        "tts": "alphapilot_broker_tts.gateway",
    }

    for broker in sorted(require):
        # 1) compiled SDK bindings importable?
        module_path = sdk_flags.get(broker)
        if module_path is not None:
            try:
                module = __import__(module_path, fromlist=["SDK_AVAILABLE"])
                if module.SDK_AVAILABLE:
                    results[f"{broker}: compiled SDK bindings"] = "OK"
                else:
                    results[f"{broker}: compiled SDK bindings"] = "FAIL (SDK_AVAILABLE=False)"
                    failed = True
            except Exception:
                results[f"{broker}: compiled SDK bindings"] = "FAIL\n" + traceback.format_exc()
                failed = True

        # 2) native gateway resolves + constructs (creates the C++ API objects)?
        try:
            if broker == "tts":
                gateway = create_gateway("tts")
                quote_gateway = create_quote_gateway("tts_7x24")
                assert isinstance(gateway, BrokerGateway)
                assert isinstance(quote_gateway, QuoteGateway)
                assert gateway is not quote_gateway
                results["tts: trade-only gateway construct (TtsTradeGateway)"] = "OK"
                results["tts: replay quote-only gateway construct (TtsReplayQuoteGateway)"] = "OK"
            else:
                gateway, quote_gateway = create_gateway_pair(broker, broker)
                assert isinstance(gateway, BrokerGateway)
                assert quote_gateway is gateway
                results[f"{broker}: gateway construct ({type(gateway).__name__})"] = "OK"
        except Exception:
            results[f"{broker}: gateway construct"] = "FAIL\n" + traceback.format_exc()
            failed = True

    # 3) strategy-integration layer imports (runner/adapter/aggregator).
    try:
        from alphapilot.systems.live.bars import BarAggregator  # noqa: F401
        from alphapilot.systems.live.strategy_runner import LiveTimingRunner  # noqa: F401
        from alphapilot.systems.timing.live_adapter import BatchStrategyAdapter  # noqa: F401

        results["strategy runner imports"] = "OK"
    except Exception:
        results["strategy runner imports"] = "FAIL\n" + traceback.format_exc()
        failed = True

    print("=" * 60)
    for key, value in results.items():
        print(f"{key}: {value}")
    print("=" * 60)
    print("SMOKE", "FAILED" if failed else "PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
