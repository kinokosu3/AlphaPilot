"""Real Chrome smoke coverage for every Portal route and a rendered backtest chart."""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pandas as pd
import pytest
import uvicorn

from alphapilot.modules.portal.api import create_app


ROUTES = (
    "/",
    "/mining",
    "/backtest",
    "/timing",
    "/library",
    "/market",
    "/daily-trade",
    "/live",
    "/scheduler",
    "/notifications",
    "/advanced",
)


@pytest.fixture()
def browser_portal_url(isolated_env):  # noqa: ANN001
    static_dir = Path(__file__).parents[1] / "alphapilot" / "modules" / "portal" / "web" / "dist"
    if not (static_dir / "index.html").is_file():
        pytest.skip("Portal production build is required (run npm run build)")

    # Seed one fully readable legacy result so the browser exercises the details
    # endpoint and dynamically loads the real candlestick-capable chart bundle.
    workspace = isolated_env.workspace_root / "browser-demo"
    workspace.mkdir()
    index = pd.date_range("2026-01-05", periods=4, freq="B")
    pd.DataFrame(
        {
            "return": [0.01, -0.002, 0.004, 0.003],
            "bench": [0.003, -0.001, 0.002, 0.001],
            "cost": [0.0002, 0.0001, 0.0001, 0.0001],
            "turnover": [0.2, 0.1, 0.12, 0.08],
            "account": [10100.0, 10079.8, 10119.1, 10148.4],
            "value": [5000.0, 4900.0, 5100.0, 5050.0],
            "cash": [5100.0, 5179.8, 5019.1, 5098.4],
        },
        index=index,
    ).to_pickle(workspace / "ret.pkl")

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    app = create_app(static_dir=static_dir)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/status", timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(0.1)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("Portal browser test server did not start")

    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        engine = getattr(app.state, "engine", None)
        if engine is not None:
            engine.shutdown()


def test_all_portal_pages_render_in_real_chrome_without_api_500(browser_portal_url: str) -> None:
    if not shutil.which("google-chrome"):
        pytest.skip("google-chrome is not installed")

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as ec
    from selenium.webdriver.support.ui import WebDriverWait

    options = Options()
    options.binary_location = shutil.which("google-chrome")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1600,1200")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    paper_daemon_started = False
    try:
        for route in ROUTES:
            driver.get(f"{browser_portal_url}{route}")
            WebDriverWait(driver, 15).until(ec.presence_of_element_located((By.CSS_SELECTOR, ".shell")))
            WebDriverWait(driver, 15).until(
                lambda current: not current.find_elements(By.CSS_SELECTOR, ".route-skeleton")
            )
            assert "AlphaPilot" in driver.find_element(By.TAG_NAME, "body").text

        # The live route is a compact trading workspace: first visit is Paper,
        # the strategy/order cards share the desktop first screen, and only one
        # business tab panel is mounted at a time.
        driver.get(f"{browser_portal_url}/live")
        WebDriverWait(driver, 15).until(ec.presence_of_element_located((By.CSS_SELECTOR, ".live-workspace-status")))
        selected_environment = driver.find_element(By.CSS_SELECTOR, ".live-environment-tabs [aria-selected='true']")
        assert "PAPER" in selected_environment.text.upper()
        assert len(driver.find_elements(By.CSS_SELECTOR, ".live-workbench-grid > .live-work-card")) == 2
        desktop_columns = driver.execute_script(
            "return getComputedStyle(document.querySelector('.live-workbench-grid')).gridTemplateColumns"
        )
        assert len(str(desktop_columns).split()) == 2
        assert len(driver.find_elements(By.CSS_SELECTOR, ".live-tab-content [role='tabpanel']")) == 0
        assert driver.execute_script("return document.documentElement.scrollHeight") <= 2200

        start_request = urllib.request.Request(
            f"{browser_portal_url}/api/live/daemon/start",
            data=json.dumps(
                {
                    "mode": "paper",
                    "broker": "paper",
                    "trade_broker": "paper",
                    "quote_provider": "paper",
                    "cash": 100000,
                    "symbols": "600000",
                    "record_market_data": False,
                    "timeout": 20,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(start_request, timeout=30) as response:
            assert response.status == 200
        paper_daemon_started = True
        driver.refresh()
        WebDriverWait(driver, 15).until(ec.presence_of_element_located((By.CSS_SELECTOR, ".live-workspace-status .pill.running")))

        driver.set_window_size(390, 844)
        WebDriverWait(driver, 10).until(
            lambda current: len(
                str(
                    current.execute_script(
                        "return getComputedStyle(document.querySelector('.live-workbench-grid')).gridTemplateColumns"
                    )
                ).split()
            ) == 1
        )
        assert driver.find_element(By.CSS_SELECTOR, ".live-status-actions .danger").is_displayed()
        assert driver.find_element(By.CSS_SELECTOR, ".live-business-tabs").is_displayed()
        driver.set_window_size(1600, 1200)

        # Open the seeded result. This verifies the backtest-detail UI and that the
        # reduced Plotly finance bundle can actually render the existing traces.
        driver.get(f"{browser_portal_url}/backtest")
        buttons = WebDriverWait(driver, 15).until(
            lambda current: [
                button
                for button in current.find_elements(By.CSS_SELECTOR, ".row-actions button")
                if button.text in {"打开", "Open"}
            ]
        )
        buttons[0].click()
        WebDriverWait(driver, 20).until(ec.presence_of_element_located((By.CSS_SELECTOR, ".js-plotly-plot")))

        api_failures: list[tuple[int, str]] = []
        for entry in driver.get_log("performance"):
            message = json.loads(entry["message"])["message"]
            if message.get("method") != "Network.responseReceived":
                continue
            response = message["params"]["response"]
            status = int(response.get("status") or 0)
            url = str(response.get("url") or "")
            if url.startswith(f"{browser_portal_url}/api/") and status >= 500:
                api_failures.append((status, url))
        assert api_failures == []

        severe = [
            entry["message"]
            for entry in driver.get_log("browser")
            if entry.get("level") == "SEVERE"
            and "/favicon.ico" not in entry.get("message", "")
            # Chrome reports expected 4xx business responses as console errors;
            # HTTP status correctness is asserted from the network log above.
            and "Failed to load resource" not in entry.get("message", "")
        ]
        assert severe == []
    finally:
        if paper_daemon_started:
            try:
                stop_request = urllib.request.Request(
                    f"{browser_portal_url}/api/live/daemon/stop",
                    data=b'{"timeout": 5}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(stop_request, timeout=10).close()
            except OSError:
                pass
        driver.quit()
