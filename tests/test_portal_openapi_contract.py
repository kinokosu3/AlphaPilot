"""Stable contract snapshot for the complete Portal HTTP surface."""

from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from alphapilot.modules.portal.api import create_app


EXPECTED_OPERATION_COUNT = 133
EXPECTED_PATH_COUNT = 118
EXPECTED_CONTRACT_SHA256 = "6e3d82af43cab9ee52974ecc44a33d11d8efc04d9f36f55fce2e66505b3781c8"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _operation_contract(spec: dict) -> list[dict]:
    operations = []
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operations.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "operation_id": operation.get("operationId"),
                    "request_body_required": bool(
                        operation.get("requestBody", {}).get("required")
                    ),
                    "responses": sorted(operation.get("responses", {})),
                }
            )
    return sorted(operations, key=lambda item: (item["path"], item["method"]))


def test_openapi_operation_snapshot_is_exact() -> None:
    spec = create_app().openapi()
    contract = _operation_contract(spec)
    encoded = json.dumps(
        contract,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert len(spec["paths"]) == EXPECTED_PATH_COUNT
    assert len(contract) == EXPECTED_OPERATION_COUNT
    assert len({item["operation_id"] for item in contract}) == EXPECTED_OPERATION_COUNT
    assert hashlib.sha256(encoded).hexdigest() == EXPECTED_CONTRACT_SHA256


def test_all_typed_operations_declare_validation_errors() -> None:
    spec = create_app().openapi()
    for item in _operation_contract(spec):
        operation = spec["paths"][item["path"]][item["method"].lower()]
        has_typed_input = bool(
            operation.get("parameters") or operation.get("requestBody")
        )
        if has_typed_input:
            assert "422" in operation["responses"], item
        assert "200" in operation["responses"], item


def test_representative_resource_and_validation_failures_never_return_500(isolated_env) -> None:
    client = TestClient(create_app())
    probes = [
        ("GET", "/api/jobs/missing/log", None),
        ("GET", "/api/jobs/missing/result", None),
        ("GET", "/api/backtests/missing", None),
        ("GET", "/api/trade-sessions/missing", None),
        ("POST", "/api/factors", {}),
        ("POST", "/api/jobs", {}),
        ("PATCH", "/api/factors/missing", {}),
    ]
    for method, path, body in probes:
        response = client.request(method, path, json=body)
        assert response.status_code in {400, 404, 422}, (method, path, response.text)
