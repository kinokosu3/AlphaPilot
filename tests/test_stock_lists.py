from pathlib import Path

from alphapilot.systems.data.stock_list import load_stocks_from_file


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_qa_stock_pool_contains_30_unique_symbols() -> None:
    """Keep the named QA universe aligned with its advertised size."""
    path = REPO_ROOT / "important_data" / "stock_lists" / "test_stock_pool_30.csv"

    symbols = load_stocks_from_file(path)

    assert len(symbols) == 30
    assert len(set(symbols)) == 30
    assert {
        "sh.600000",
        "sh.600085",
        "sh.600188",
        "sh.600519",
        "sh.600588",
        "sh.600711",
    } <= set(symbols)
