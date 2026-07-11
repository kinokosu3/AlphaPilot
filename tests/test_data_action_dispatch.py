from typing import Any

from alphapilot.systems.data.pipeline import dispatch_prepare_action


def _capture(calls: list[dict[str, Any]]):
    def handler(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return kwargs

    return handler


def test_convert_forwards_common_interface_parameters() -> None:
    calls: list[dict[str, Any]] = []
    handler = _capture(calls)

    dispatch_prepare_action(
        action="convert",
        download_handler=handler,
        convert_handler=handler,
        pipeline_handler=handler,
        start_date="2009-01-01",
        end_date="2026-07-09",
        stock_csv="pool.csv",
        adjust_mode="backward",
        market="qa_stock_pool_30",
        qlib_dir="/qa/qlib",
        output_dir="/qa/raw",
        max_workers=4,
    )

    assert calls == [
        {
            "adjust_mode": "backward",
            "start_date": "2009-01-01",
            "end_date": "2026-07-09",
            "max_workers": 4,
            "stock_csv": "pool.csv",
            "market": "qa_stock_pool_30",
            "qlib_dir": "/qa/qlib",
            "data_path": "/qa/raw",
        }
    ]


def test_pipeline_forwards_market_and_output_roots() -> None:
    calls: list[dict[str, Any]] = []
    handler = _capture(calls)

    dispatch_prepare_action(
        action="pipeline",
        download_handler=handler,
        convert_handler=handler,
        pipeline_handler=handler,
        start_date="2009-01-01",
        end_date="2026-07-09",
        stock_csv="pool.csv",
        adjust_mode="none",
        market="qa_stock_pool_30",
        qlib_dir="/qa/qlib",
        output_dir="/qa/raw",
    )

    assert calls[0] == {
        "start_date": "2009-01-01",
        "end_date": "2026-07-09",
        "adjust_mode": "none",
        "stock_csv": "pool.csv",
        "market": "qa_stock_pool_30",
        "qlib_dir": "/qa/qlib",
        "data_dir": "/qa/raw",
    }


def test_calendar_forwards_date_window_without_unrelated_options(monkeypatch) -> None:  # noqa: ANN001
    calls: list[dict[str, Any]] = []

    class FakePrepareDataCLI:
        def calendar(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(
        "alphapilot.systems.data.prepare_data.PrepareDataCLI",
        FakePrepareDataCLI,
    )
    handler = _capture([])

    dispatch_prepare_action(
        action="calendar",
        download_handler=handler,
        convert_handler=handler,
        pipeline_handler=handler,
        start_date="2026-01-01",
        end_date="2026-12-31",
        stock_csv="must-not-be-forwarded.csv",
        market="must-not-be-forwarded",
        qlib_dir="/qa/qlib",
    )

    assert calls == [
        {
            "qlib_dir": "/qa/qlib",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        }
    ]
