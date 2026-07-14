"""Immutable snapshots that turn research assets into deployable inputs."""

from __future__ import annotations

from dataclasses import asdict
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from alphapilot.systems.trading.contracts import canonical_instrument
from alphapilot.systems.trading.security import verify_trusted_model


class ResearchArtifactSnapshotter:
    def __init__(self, root: str | Path, *, strategy_system: Any, config: Any) -> None:
        self.root = Path(root).expanduser()
        self.strategy_system = strategy_system
        self.config = config

    def snapshot(
        self,
        *,
        strategy_name: str,
        instance_id: str,
        universe: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        record = self.strategy_system.get_strategy(strategy_name)
        if record is None:
            raise KeyError(f"unknown research strategy asset {strategy_name!r}")
        model_uri = getattr(getattr(record, "model", None), "trained_artifact_uri", None)
        if not model_uri:
            raise ValueError("research strategy has no trained model artifact")
        strategy_dir = self.strategy_system.param_database.strategy_dir(strategy_name)
        extra_roots = [strategy_dir] if strategy_dir is not None else []
        model_hash = verify_trusted_model(model_uri, extra_roots=extra_roots)
        members = self._resolve_universe(record, universe)
        if not members:
            raise ValueError("selection strategy universe must not be empty")
        template_source: Path | None = None
        template_hash = ""
        template_name = str((record.metadata or {}).get("qlib_template_snapshot_dir") or "")
        if template_name:
            if strategy_dir is None:
                raise ValueError("research strategy has no directory for its Qlib template")
            strategy_root = Path(strategy_dir).expanduser().resolve()
            template_source = (strategy_root / template_name).resolve()
            try:
                template_source.relative_to(strategy_root)
            except ValueError as exc:
                raise ValueError("Qlib template must remain inside the research strategy directory") from exc
            if not template_source.is_dir():
                raise ValueError("declared Qlib template snapshot directory is missing")
            template_hash = _tree_sha256(template_source)
        record_payload = asdict(record)
        research_fingerprint = _digest_json({
            "record": record_payload,
            "model_hash": model_hash,
            "qlib_template_hash": template_hash,
            "universe": members,
        })
        destination = self.root / _safe(instance_id) / research_fingerprint
        manifest_path = destination / "manifest.json"
        if manifest_path.is_file():
            return json.loads(manifest_path.read_text(encoding="utf-8"))

        self.root.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=f".{_safe(instance_id)}-", dir=self.root))
        try:
            model_source = Path(model_uri).expanduser().resolve()
            model_destination = staged / "model" / model_source.name
            model_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(model_source, model_destination)
            factors_path = staged / "factors.csv"
            with factors_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["factor_name", "factor_expression"])
                writer.writeheader()
                for index, formula in enumerate(record.factor_formulas, 1):
                    writer.writerow({
                        "factor_name": f"factor_{index:03d}",
                        "factor_expression": formula,
                    })
            yaml_path = staged / "yaml_params.json"
            yaml_params = dict((record.metadata or {}).get("yaml_params") or {})
            yaml_path.write_text(json.dumps(yaml_params, ensure_ascii=False, indent=2), encoding="utf-8")

            template_destination: Path | None = None
            if template_source is not None:
                template_destination = staged / "qlib_template"
                shutil.copytree(template_source, template_destination)

            manifest = {
                "artifact_type": "qlib_selection",
                "research_asset": strategy_name,
                "research_fingerprint": research_fingerprint,
                "model_hash": model_hash,
                "factor_hash": _sha256(factors_path),
                "model_path": str(destination / "model" / model_source.name),
                "factor_path": str(destination / "factors.csv"),
                "yaml_params": yaml_params,
                "qlib_template_dir": (
                    str(destination / "qlib_template") if template_destination is not None else None
                ),
                "qlib_template_hash": template_hash,
                "provider_uri": str(self.config.data.qlib_data_dir),
                "use_local": bool(self.config.backtest.use_local),
                "market": (record.metadata or {}).get("market"),
                "universe": members,
            }
            (staged / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staged, destination)
            except OSError:
                if not destination.is_dir():
                    raise
                shutil.rmtree(staged, ignore_errors=True)
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            shutil.rmtree(staged, ignore_errors=True)
            raise

    def _resolve_universe(self, record: Any, supplied: Any) -> list[str]:
        if supplied:
            return sorted({canonical_instrument(item) for item in supplied})
        market = str((record.metadata or {}).get("market") or "").strip()
        if not market:
            return []
        try:
            from alphapilot.systems.data.stock_pool import StockPoolRepository

            pool = StockPoolRepository(self.config.data).get_pool(market)
            symbols = pool.get("symbols") or []
            if symbols:
                return sorted({canonical_instrument(item) for item in symbols})
        except Exception:
            pass
        instrument_file = Path(self.config.data.qlib_data_dir) / "instruments" / f"{market}.txt"
        if not instrument_file.is_file():
            return []
        symbols = [
            line.split()[0] for line in instrument_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return sorted({canonical_instrument(item) for item in symbols})


def verify_artifact_binding(
    binding: dict[str, Any],
    *,
    snapshot_root: str | Path | None = None,
    expected_instance_id: str | None = None,
) -> None:
    if binding.get("artifact_type") != "qlib_selection":
        raise ValueError("unsupported artifact binding")
    model = Path(str(binding.get("model_path") or "")).expanduser()
    factors = Path(str(binding.get("factor_path") or "")).expanduser()
    if not model.is_file() or _sha256(model) != str(binding.get("model_hash") or ""):
        raise ValueError("snapshotted model is missing or has changed")
    if not factors.is_file() or _sha256(factors) != str(binding.get("factor_hash") or ""):
        raise ValueError("snapshotted factors are missing or have changed")
    template_value = binding.get("qlib_template_dir")
    template_hash = str(binding.get("qlib_template_hash") or "")
    template: Path | None = None
    if template_value:
        template = Path(str(template_value)).expanduser()
        if not template_hash:
            raise ValueError("snapshotted Qlib template has no content hash")
        if not template.is_dir() or _tree_sha256(template) != template_hash:
            raise ValueError("snapshotted Qlib template is missing or has changed")
    elif template_hash:
        raise ValueError("Qlib template hash has no matching directory")
    if snapshot_root is None:
        return
    root = Path(snapshot_root).expanduser().resolve()
    model = model.resolve()
    factors = factors.resolve()
    try:
        model.relative_to(root)
        factors.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact paths are outside the immutable snapshot root") from exc
    destination = model.parent.parent
    if factors.parent != destination:
        raise ValueError("model and factors do not belong to the same artifact snapshot")
    if template is not None and template.resolve().parent != destination:
        raise ValueError("Qlib template does not belong to the same artifact snapshot")
    if expected_instance_id is not None:
        expected_parent = root / _safe(expected_instance_id)
        if destination.parent != expected_parent.resolve():
            raise ValueError("artifact snapshot is bound to a different strategy instance")
    manifest_path = destination / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("artifact snapshot manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest != binding:
        raise ValueError("artifact binding does not match its immutable manifest")


def _digest_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    """Hash every regular file and relative path in an immutable template tree."""

    base = root.expanduser().resolve()
    digest = hashlib.sha256(b"alphapilot-tree-v1\0")
    for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(base).as_posix()):
        if path.is_symlink():
            raise ValueError("Qlib template snapshots cannot contain symbolic links")
        if not path.is_file():
            continue
        relative = path.relative_to(base).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _safe(value: str) -> str:
    normalized = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return normalized.strip("._") or "instance"
