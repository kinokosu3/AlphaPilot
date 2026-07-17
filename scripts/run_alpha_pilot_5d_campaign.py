#!/usr/bin/env python3
"""Run or inspect the frozen AlphaPilot five-day research campaign."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from alphapilot.systems.research.campaign import CampaignRunner, DEFAULT_MANIFEST


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage",
        choices=[
            "status",
            "data-build",
            "preflight",
            "llm",
            "rl-smoke",
            "rl-production",
            "all-research",
            "record-gate",
            "build-whitelist",
            "freeze-asset",
            "sync-evidence",
        ],
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--gate-stage", default="")
    parser.add_argument("--gate-json", default="")
    parser.add_argument("--source-strategy", default="")
    parser.add_argument("--save-as", default="")
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--account-id", default="")
    parser.add_argument("--broker", default="xtp")
    parser.add_argument("--factor-names", default="")
    parser.add_argument("--whitelist", default="")
    parser.add_argument("--bars", default="")
    parser.add_argument("--stock-basic", default="")
    parser.add_argument("--account-equity", type=float, default=0.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    runner = CampaignRunner(Path(args.manifest))
    if args.stage == "status":
        result = runner.status()
    elif args.stage == "data-build":
        result = runner.build_data(force=args.force)
    elif args.stage == "preflight":
        result = runner.preflight(force=args.force)
    elif args.stage == "llm":
        result = runner.run_llm(force=args.force)
    elif args.stage == "rl-smoke":
        result = runner.run_rl_smoke(force=args.force)
    elif args.stage == "rl-production":
        result = runner.run_rl_production(force=args.force)
    elif args.stage == "all-research":
        runner.preflight(force=args.force)
        result = {
            "llm": runner.run_llm(force=args.force),
            "rl_smoke": runner.run_rl_smoke(force=args.force),
            "rl_production": runner.run_rl_production(force=args.force),
        }
    elif args.stage == "build-whitelist":
        if not all((args.bars, args.stock_basic, args.output)) or args.account_equity <= 0:
            parser.error(
                "build-whitelist requires --bars, --stock-basic, --account-equity and --output"
            )
        import pandas as pd

        from alphapilot.systems.research.whitelist import (
            build_live_whitelist,
            freeze_whitelist,
        )

        bars_path = Path(args.bars).expanduser()
        bars = (
            pd.read_parquet(bars_path)
            if bars_path.suffix.lower() in {".parquet", ".pq"}
            else pd.read_csv(bars_path)
        )
        if not isinstance(bars.index, pd.MultiIndex):
            if not {"datetime", "instrument"} <= set(bars.columns):
                parser.error("bars must contain datetime and instrument columns")
            bars["datetime"] = pd.to_datetime(bars["datetime"])
            bars = bars.set_index(["datetime", "instrument"]).sort_index()
        basic_path = Path(args.stock_basic).expanduser()
        stock_basic = (
            pd.read_parquet(basic_path)
            if basic_path.suffix.lower() in {".parquet", ".pq"}
            else pd.read_csv(basic_path)
        )
        deployment = runner.manifest["deployment"]
        payload = build_live_whitelist(
            bars,
            stock_basic,
            account_equity=args.account_equity,
            as_of=runner.manifest["as_of"],
            top_n=deployment["whitelist_size"],
            liquidity_days=deployment["whitelist_liquidity_days"],
            minimum_trading_age=deployment["minimum_trading_age"],
            lot_size=deployment["lot_size"],
            max_lot_equity_ratio=deployment["max_one_lot_equity_ratio"],
        )
        output = freeze_whitelist(payload, args.output)
        result = {**payload, "output_path": str(output)}
    elif args.stage == "freeze-asset":
        if not all(
            (args.source_strategy, args.save_as, args.instance_id, args.whitelist)
        ):
            parser.error(
                "freeze-asset requires --source-strategy, --save-as, --instance-id and --whitelist"
            )
        result = runner.freeze_deployment_asset(
            source_strategy_name=args.source_strategy,
            save_as=args.save_as,
            instance_id=args.instance_id,
            whitelist=args.whitelist,
            factor_names=[
                item.strip() for item in args.factor_names.split(",") if item.strip()
            ]
            or None,
        )
    elif args.stage == "sync-evidence":
        if not args.instance_id:
            parser.error("sync-evidence requires --instance-id")
        result = runner.sync_forward_evidence(
            instance_id=args.instance_id,
            account_id=args.account_id,
            broker=args.broker,
        )
    else:
        if not args.gate_stage or not args.gate_json:
            parser.error("record-gate requires --gate-stage and --gate-json")
        result = runner.record_gate(
            args.gate_stage,
            json.loads(Path(args.gate_json).read_text(encoding="utf-8")),
        )
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
