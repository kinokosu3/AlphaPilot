"""Built-in, local-manifest and pip entry-point strategy discovery."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
from importlib.metadata import entry_points
import inspect
import json
from pathlib import Path
from typing import Any, Iterable

from alphapilot.systems.trading.domain import StrategyDefinition

STRATEGY_ENTRY_POINT_GROUP = "alphapilot.strategies"
STRATEGY_API_VERSION = 1


@dataclass(frozen=True)
class QuarantinedStrategy:
    source: str
    reason: str
    strategy_id: str = ""


class StrategyRegistry:
    def __init__(self, *, local_root: str | Path | None = None) -> None:
        self.local_root = Path(local_root or (Path.cwd() / "strategies")).expanduser()
        self._definitions: dict[str, StrategyDefinition] = {}
        self._quarantined: list[QuarantinedStrategy] = []
        self._local_bases: dict[str, Path] = {}

    def discover(self) -> "StrategyRegistry":
        self._definitions.clear()
        self._quarantined.clear()
        self._local_bases.clear()
        for definition in builtin_definitions():
            self._register(definition)
        self._discover_local()
        self._discover_entry_points()
        return self

    def list(self) -> list[StrategyDefinition]:
        return [self._definitions[key] for key in sorted(self._definitions)]

    def get(self, strategy_id: str) -> StrategyDefinition:
        key = str(strategy_id).strip().lower()
        if key not in self._definitions:
            raise KeyError(f"unknown strategy definition {strategy_id!r}")
        return self._definitions[key]

    def create(
        self,
        strategy_id: str,
        params: dict[str, Any] | None = None,
        *,
        isolated: bool | None = None,
    ) -> Any:
        definition = self.get(strategy_id)
        merged = schema_defaults(definition.parameter_schema)
        merged.update(params or {})
        errors = validate_parameters(definition.parameter_schema, merged)
        if errors:
            raise ValueError("; ".join(errors))
        if isolated is None:
            isolated = definition.source != "builtin"
        if isolated:
            from alphapilot.systems.trading.worker import IsolatedBatchStrategy

            return IsolatedBatchStrategy(
                definition.to_dict()["factory"], merged,
                base=self._local_bases.get(definition.strategy_id),
            )
        factory = definition.factory
        if isinstance(factory, str):
            factory = _load_factory(factory, base=self._local_bases.get(definition.strategy_id))
        return factory(**merged)

    def quarantined(self) -> list[dict[str, str]]:
        return [item.__dict__ for item in self._quarantined]

    def _register(self, definition: StrategyDefinition, *, base: Path | None = None) -> None:
        key = str(definition.strategy_id).strip().lower()
        if not key:
            raise ValueError("strategy_id is required")
        if key in self._definitions:
            self._quarantined.append(
                QuarantinedStrategy(definition.source, "duplicate strategy_id; earlier source wins", key)
            )
            return
        if int(definition.state_schema_version) <= 0:
            raise ValueError("state_schema_version must be positive")
        if int(definition.api_version) != STRATEGY_API_VERSION:
            raise ValueError(
                f"strategy api version {definition.api_version} is unsupported; "
                f"expected {STRATEGY_API_VERSION}"
            )
        definition.strategy_id = key
        self._definitions[key] = definition
        if base is not None:
            self._local_bases[key] = base

    def _discover_local(self) -> None:
        if not self.local_root.is_dir():
            return
        for manifest in sorted(self.local_root.glob("*/strategy.toml")):
            try:
                data = _read_toml(manifest)
                section = data.get("strategy") or data
                api_version = int(section.get("api_version", STRATEGY_API_VERSION))
                if api_version != STRATEGY_API_VERSION:
                    raise ValueError(f"strategy api version {api_version} is unsupported")
                schema = section.get("parameter_schema") or {}
                if section.get("parameter_schema_json"):
                    schema = json.loads(str(section["parameter_schema_json"]))
                definition = StrategyDefinition(
                    strategy_id=str(section["id"]),
                    version=str(section.get("version") or "0.1.0"),
                    kind=str(section.get("kind") or "rule"),
                    factory=str(section["factory"]),
                    parameter_schema=dict(schema),
                    supported_assets=tuple(section.get("supported_assets") or ("equity", "fund")),
                    supported_frequencies=tuple(section.get("supported_frequencies") or ("day", "min")),
                    output_type=str(section.get("output_type") or "signals"),
                    required_history=int(section.get("required_history") or 1),
                    state_schema_version=int(section.get("state_schema_version") or 1),
                    source=f"local:{manifest}",
                    code_hash=_manifest_code_hash(manifest, str(section["factory"])),
                    description=str(section.get("description") or ""),
                    api_version=api_version,
                )
                self._register(definition, base=manifest.parent)
            except Exception as exc:  # noqa: BLE001 - bad plugins are isolated
                self._quarantined.append(
                    QuarantinedStrategy(str(manifest), f"{type(exc).__name__}: {exc}")
                )

    def _discover_entry_points(self) -> None:
        try:
            eps = list(entry_points(group=STRATEGY_ENTRY_POINT_GROUP))
        except TypeError:  # pragma: no cover - old importlib.metadata
            eps = list(entry_points().get(STRATEGY_ENTRY_POINT_GROUP, []))
        for ep in sorted(eps, key=lambda item: item.name):
            try:
                loaded = ep.load()
                value = loaded() if callable(loaded) and not isinstance(loaded, type) else loaded
                definitions = value if isinstance(value, (list, tuple)) else [value]
                for definition in definitions:
                    if not isinstance(definition, StrategyDefinition):
                        raise TypeError("entry point must return StrategyDefinition or a list of them")
                    distribution = getattr(ep, "dist", None)
                    package_version = str(getattr(distribution, "version", "") or "")
                    definition.source = f"pip:{distribution or ep.name}"
                    definition.package_version = definition.package_version or package_version
                    if not definition.code_hash:
                        definition.code_hash = _entry_point_code_hash(ep, definition)
                    if not definition.code_hash:
                        raise ValueError("installed strategy code could not be hashed")
                    self._register(definition)
            except Exception as exc:  # noqa: BLE001
                self._quarantined.append(
                    QuarantinedStrategy(f"pip:{ep.name}", f"{type(exc).__name__}: {exc}")
                )


def builtin_definitions() -> list[StrategyDefinition]:
    from alphapilot.systems.timing.strategies import _STRATEGY_CLASSES

    definitions: list[StrategyDefinition] = []
    for cls in _STRATEGY_CLASSES:
        schema = _schema_from_defaults(cls.defaults)
        required = _required_history(cls.defaults)
        definitions.append(
            StrategyDefinition(
                strategy_id=cls.name,
                version="1.0.0",
                kind="rule",
                factory=cls,
                parameter_schema=schema,
                required_history=required,
                source="builtin",
                code_hash=_source_hash(cls),
                description=cls.description,
            )
        )
    return definitions


def schema_defaults(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        key: spec["default"]
        for key, spec in (schema.get("properties") or {}).items()
        if isinstance(spec, dict) and "default" in spec
    }


def resolve_required_history(
    definition: StrategyDefinition | None,
    params: dict[str, Any] | None,
    *,
    fallback: int = 1,
) -> int:
    """Resolve warmup bars from the concrete instance parameters."""

    values = params or {}
    windows = {
        key: int(value)
        for key, value in values.items()
        if "window" in key and isinstance(value, int) and not isinstance(value, bool) and value > 0
    }
    if "rsi_window" in windows and "stoch_window" in windows:
        return windows["rsi_window"] + windows["stoch_window"] + 1
    if windows:
        return max(windows.values()) + 1
    return max(int(getattr(definition, "required_history", 0) or fallback or 1), 1)


def validate_parameters(schema: dict[str, Any], params: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    for key in sorted(required - set(params)):
        errors.append(f"params.{key} is required")
    if schema.get("additionalProperties") is False:
        for key in sorted(set(params) - set(properties)):
            errors.append(f"params.{key} is not supported")
    for key, value in params.items():
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            errors.append(f"params.{key} must be an integer")
            continue
        if expected == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            errors.append(f"params.{key} must be a number")
            continue
        if expected == "boolean" and not isinstance(value, bool):
            errors.append(f"params.{key} must be a boolean")
            continue
        if expected == "string" and not isinstance(value, str):
            errors.append(f"params.{key} must be a string")
            continue
        if isinstance(value, (int, float)):
            if "minimum" in spec and value < spec["minimum"]:
                errors.append(f"params.{key} must be >= {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                errors.append(f"params.{key} must be <= {spec['maximum']}")
    if "short_window" in params and "long_window" in params:
        if int(params["short_window"]) >= int(params["long_window"]):
            errors.append("params.short_window must be less than params.long_window")
    return errors


def _schema_from_defaults(defaults: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for key, value in defaults.items():
        if isinstance(value, bool):
            spec = {"type": "boolean", "default": value}
        elif isinstance(value, int):
            spec = {"type": "integer", "default": value}
        elif isinstance(value, float):
            spec = {"type": "number", "default": value}
        else:
            spec = {"type": "string", "default": value}
        if "window" in key:
            spec["minimum"] = 1
        if key == "target_percent":
            spec.update({"minimum": 0.0, "maximum": 1.0})
        properties[key] = spec
    return {"type": "object", "properties": properties, "additionalProperties": False}


def _required_history(defaults: dict[str, Any]) -> int:
    windows = [int(value) for key, value in defaults.items() if "window" in key and isinstance(value, int)]
    return max(windows or [1]) + 1


def _load_factory(path: str, *, base: Path | None = None) -> Any:
    module_name, sep, attr = path.partition(":")
    if not sep:
        module_name, _, attr = path.rpartition(".")
    if base is not None and (base / f"{module_name}.py").is_file():
        file_path = base / f"{module_name}.py"
        unique = f"alphapilot_local_strategy_{hashlib.sha256(str(file_path).encode()).hexdigest()[:12]}"
        spec = importlib.util.spec_from_file_location(unique, file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load local strategy module {file_path}")
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
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _source_hash(obj: Any) -> str:
    path = inspect.getsourcefile(obj)
    if not path:
        return ""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _manifest_code_hash(manifest: Path, factory: str) -> str:
    module_name = factory.split(":", 1)[0]
    source = manifest.parent / f"{module_name}.py"
    digest = hashlib.sha256(manifest.read_bytes())
    if source.is_file():
        digest.update(source.read_bytes())
    return digest.hexdigest()


def _entry_point_code_hash(ep: Any, definition: StrategyDefinition) -> str:
    """Hash installed code/metadata rather than trusting a version label alone."""

    digest = hashlib.sha256()
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
        digest.update(str(path.name).encode("utf-8"))
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
