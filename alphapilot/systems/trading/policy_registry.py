"""Discovery for portfolio-construction policies.

Policies are deliberately registered separately from signal providers.  This
keeps model/rule code unaware of account sizing and makes future composition a
policy concern rather than a broker concern.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
from importlib.metadata import entry_points
import inspect
import json
from pathlib import Path
from typing import Any

from alphapilot.systems.trading.contracts import SignalKind
from alphapilot.systems.trading.domain import PortfolioPolicyDefinition
from alphapilot.systems.trading.portfolio import (
    SelectionTopKDropoutEqualWeightPolicy,
    TimingFixedExposurePolicy,
)
from alphapilot.systems.trading.registry import schema_defaults, validate_parameters

POLICY_ENTRY_POINT_GROUP = "alphapilot.portfolio_policies"
POLICY_API_VERSION = 1


class PortfolioPolicyRegistry:
    def __init__(self, *, local_root: str | Path | None = None) -> None:
        self.local_root = Path(local_root or (Path.cwd() / "policies")).expanduser()
        self._definitions: dict[str, PortfolioPolicyDefinition] = {}
        self._bases: dict[str, Path] = {}
        self._quarantined: list[dict[str, str]] = []

    def discover(self) -> "PortfolioPolicyRegistry":
        self._definitions.clear()
        self._bases.clear()
        self._quarantined.clear()
        for definition in builtin_policy_definitions():
            self._register(definition)
        self._discover_local()
        self._discover_entry_points()
        return self

    def list(self) -> list[PortfolioPolicyDefinition]:
        return [self._definitions[key] for key in sorted(self._definitions)]

    def get(self, policy_id: str) -> PortfolioPolicyDefinition:
        key = str(policy_id).strip().lower()
        if key not in self._definitions:
            raise KeyError(f"unknown portfolio policy {policy_id!r}")
        return self._definitions[key]

    def create(self, policy_id: str, params: dict[str, Any] | None = None) -> Any:
        definition = self.get(policy_id)
        merged = schema_defaults(definition.parameter_schema)
        merged.update(params or {})
        errors = validate_parameters(definition.parameter_schema, merged)
        if errors:
            raise ValueError("; ".join(errors))
        factory = definition.factory
        if isinstance(factory, str):
            factory = _load_factory(factory, base=self._bases.get(definition.policy_id))
        return factory(**merged)

    def quarantined(self) -> list[dict[str, str]]:
        return list(self._quarantined)

    def _register(
        self,
        definition: PortfolioPolicyDefinition,
        *,
        base: Path | None = None,
    ) -> None:
        key = definition.policy_id
        if not key:
            raise ValueError("policy_id is required")
        if definition.api_version != POLICY_API_VERSION:
            raise ValueError(f"unsupported portfolio policy api_version={definition.api_version}")
        if key in self._definitions:
            self._quarantined.append({
                "source": definition.source,
                "policy_id": key,
                "reason": "duplicate policy_id; earlier source wins",
            })
            return
        self._definitions[key] = definition
        if base is not None:
            self._bases[key] = base

    def _discover_local(self) -> None:
        if not self.local_root.is_dir():
            return
        for manifest in sorted(self.local_root.glob("*/policy.toml")):
            try:
                section = (_read_toml(manifest).get("policy") or {})
                schema = section.get("parameter_schema") or {}
                if section.get("parameter_schema_json"):
                    schema = json.loads(str(section["parameter_schema_json"]))
                definition = PortfolioPolicyDefinition(
                    policy_id=str(section["id"]),
                    version=str(section.get("version") or "0.1.0"),
                    factory=str(section["factory"]),
                    parameter_schema=dict(schema),
                    supported_signal_kinds=tuple(
                        SignalKind(value) for value in
                        (section.get("supported_signal_kinds") or ["instrument_timing"])
                    ),
                    source=f"local:{manifest}",
                    code_hash=_manifest_hash(manifest, str(section["factory"])),
                    description=str(section.get("description") or ""),
                    api_version=int(section.get("api_version") or 1),
                )
                self._register(definition, base=manifest.parent)
            except Exception as exc:  # noqa: BLE001 - quarantine bad extensions
                self._quarantined.append({
                    "source": str(manifest),
                    "policy_id": "",
                    "reason": f"{type(exc).__name__}: {exc}",
                })

    def _discover_entry_points(self) -> None:
        try:
            eps = list(entry_points(group=POLICY_ENTRY_POINT_GROUP))
        except TypeError:  # pragma: no cover
            eps = list(entry_points().get(POLICY_ENTRY_POINT_GROUP, []))
        for ep in sorted(eps, key=lambda item: item.name):
            try:
                loaded = ep.load()
                value = loaded() if callable(loaded) and not isinstance(loaded, type) else loaded
                definitions = value if isinstance(value, (list, tuple)) else [value]
                for definition in definitions:
                    if not isinstance(definition, PortfolioPolicyDefinition):
                        raise TypeError("entry point must return PortfolioPolicyDefinition")
                    definition.source = f"pip:{getattr(ep, 'dist', None) or ep.name}"
                    definition.package_version = str(
                        getattr(getattr(ep, "dist", None), "version", "") or ""
                    )
                    # Bind the definition to the installed artifact.  A policy
                    # may expose its factory as an import string, in which case
                    # inspect.getsourcefile cannot be used directly.
                    definition.code_hash = (
                        _entry_point_code_hash(ep, definition)
                        or definition.code_hash
                        or _source_hash(definition.factory)
                    )
                    if not definition.code_hash:
                        raise ValueError("installed portfolio policy could not be hashed")
                    self._register(definition)
            except Exception as exc:  # noqa: BLE001
                self._quarantined.append({
                    "source": f"pip:{ep.name}",
                    "policy_id": "",
                    "reason": f"{type(exc).__name__}: {exc}",
                })


def builtin_policy_definitions() -> list[PortfolioPolicyDefinition]:
    return [
        PortfolioPolicyDefinition(
            policy_id="timing_fixed_exposure",
            version="1.0.0",
            factory=TimingFixedExposurePolicy,
            parameter_schema={
                "type": "object",
                "properties": {
                    "target_percent": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.2},
                    "cash_buffer": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.1},
                    "max_position_weight": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.3},
                    "exposure_mode": {
                        "type": "string",
                        "enum": ["per_instrument", "equal_active_budget"],
                    },
                },
                "additionalProperties": False,
            },
            supported_signal_kinds=(SignalKind.INSTRUMENT_TIMING,),
            code_hash=_source_hash(TimingFixedExposurePolicy),
            description="Fixed long/flat exposure for standalone timing signals.",
        ),
        PortfolioPolicyDefinition(
            policy_id="selection_topk_dropout_equal_weight",
            version="1.0.0",
            factory=SelectionTopKDropoutEqualWeightPolicy,
            parameter_schema={
                "type": "object",
                "properties": {
                    "topk": {"type": "integer", "minimum": 1, "default": 10},
                    "n_drop": {"type": "integer", "minimum": 0, "default": 2},
                    "cash_buffer": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.1},
                    "max_position_weight": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.2},
                },
                "additionalProperties": False,
            },
            supported_signal_kinds=(SignalKind.CROSS_SECTIONAL_SELECTION,),
            code_hash=_source_hash(SelectionTopKDropoutEqualWeightPolicy),
            description="Top-K bounded turnover with equal target weights.",
        ),
    ]


def _load_factory(path: str, *, base: Path | None = None) -> Any:
    module_name, _, attr = path.partition(":")
    if base is not None and (base / f"{module_name}.py").is_file():
        source = base / f"{module_name}.py"
        name = f"alphapilot_local_policy_{hashlib.sha256(str(source).encode()).hexdigest()[:12]}"
        spec = importlib.util.spec_from_file_location(name, source)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load local portfolio policy {source}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_name)
    value: Any = module
    for part in attr.split("."):
        value = getattr(value, part)
    return value


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _source_hash(factory: Any) -> str:
    if isinstance(factory, str):
        return ""
    source = inspect.getsourcefile(factory)
    return hashlib.sha256(Path(source).read_bytes()).hexdigest() if source else ""


def _manifest_hash(manifest: Path, factory: str) -> str:
    del factory
    digest = hashlib.sha256(b"alphapilot-local-policy-v1\0")
    files = [
        path for path in manifest.parent.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]
    for path in sorted(
        files,
        key=lambda item: item.relative_to(manifest.parent).as_posix(),
    ):
        if path.is_symlink():
            raise ValueError("local policy directories cannot contain symbolic links")
        relative = path.relative_to(manifest.parent).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _entry_point_code_hash(ep: Any, definition: PortfolioPolicyDefinition) -> str:
    """Hash installed policy code and distribution metadata when available."""

    digest = hashlib.sha256(b"alphapilot-installed-policy-v1\0")
    artifacts = 0
    paths: set[Path] = set()
    factory = definition.factory
    if isinstance(factory, str):
        module_name = factory.partition(":")[0]
        if ":" not in factory:
            module_name = factory.rpartition(".")[0]
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, AttributeError, ValueError):
            spec = None
        if spec is not None and spec.origin:
            paths.add(Path(spec.origin))
    else:
        source = inspect.getsourcefile(factory)
        if source:
            paths.add(Path(source))
    ep_module = str(getattr(ep, "value", "")).partition(":")[0]
    if ep_module:
        try:
            spec = importlib.util.find_spec(ep_module)
        except (ImportError, AttributeError, ValueError):
            spec = None
        if spec is not None and spec.origin:
            paths.add(Path(spec.origin))
    for path in sorted(paths, key=str):
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(payload)
        artifacts += 1

    distribution = getattr(ep, "dist", None)
    read_text = getattr(distribution, "read_text", None)
    if callable(read_text):
        for metadata_name in ("RECORD", "direct_url.json", "METADATA"):
            try:
                payload = read_text(metadata_name)
            except (OSError, UnicodeError):
                payload = None
            if not payload:
                continue
            digest.update(metadata_name.encode("utf-8"))
            digest.update(str(payload).encode("utf-8"))
            artifacts += 1
    return digest.hexdigest() if artifacts else ""
