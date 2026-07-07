"""Append-only audit ledger for the live-trading subsystem.

Every intent, submission, cancel, fill, halt and reconciliation is appended as
one JSON line. This is the durable audit trail — distinct from the OMS's
in-memory projection and from any paper/rolling state — so that after the fact you
can reconstruct exactly what the system decided and did, and when.

Append-only + line-oriented is deliberate: it is crash-safe (a torn last line is
recoverable), human-greppable, and never mutates history.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from alphapilot.systems.live.events import LiveEvent, make_event


class Ledger:
    """A per-day JSONL audit log under ``root``."""

    def __init__(self, root: str | Path, *, now_fn=datetime.now) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn

    def _path_for(self, when: datetime) -> Path:
        return self.root / f"ledger-{when:%Y%m%d}.jsonl"

    def _path_for_date(self, day: date | datetime | str) -> Path:
        if isinstance(day, datetime):
            day = day.date()
        if isinstance(day, date):
            suffix = day.strftime("%Y%m%d")
        else:
            suffix = str(day).replace("-", "")
        return self.root / f"ledger-{suffix}.jsonl"

    def record(self, kind: str, payload: Any = None) -> dict[str, Any]:
        """Append one event. ``payload`` may be a dict or a dataclass."""
        when = self._now_fn()
        if is_dataclass(payload) and not isinstance(payload, type):
            payload = asdict(payload)
        event = {"ts": when.isoformat(timespec="seconds"), "kind": kind, "payload": payload}
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._path_for(when).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return event

    def record_event(
        self,
        kind: str,
        payload: Any = None,
        *,
        command_id: str | None = None,
        order_id: str | None = None,
        reference: str | None = None,
        source: str = "live",
    ) -> dict[str, Any]:
        """Append a structured live event with common correlation fields."""
        when = self._now_fn()
        event: LiveEvent = make_event(
            kind,
            payload,
            command_id=command_id,
            order_id=order_id,
            reference=reference,
            source=source,
            now_fn=lambda: when,
        )
        return self.append_event(event)

    def append_event(self, event: LiveEvent | dict[str, Any]) -> dict[str, Any]:
        """Append a pre-built structured event.

        This lets the in-memory event bus and durable ledger share the exact
        same JSON shape. The target day is derived from ``event.ts`` when
        possible so replay/recovery filters remain stable.
        """
        data = event.to_dict() if isinstance(event, LiveEvent) else dict(event)
        when = _event_time(data.get("ts"), fallback=self._now_fn())
        line = json.dumps(data, ensure_ascii=False, default=str)
        with self._path_for(when).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return data

    def events(
        self,
        *,
        kind: str | None = None,
        command_id: str | None = None,
        order_id: str | None = None,
        reference: str | None = None,
        day: date | datetime | str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read every recorded event across all days (chronological by filename)."""
        out: list[dict[str, Any]] = []
        paths = [self._path_for_date(day)] if day is not None else sorted(self.root.glob("ledger-*.jsonl"))
        for path in paths:
            if not path.exists():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if raw:
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:  # tolerate a torn final line
                        continue
                    if _event_matches(
                        event,
                        kind=kind,
                        command_id=command_id,
                        order_id=order_id,
                        reference=reference,
                    ):
                        out.append(event)
        if limit is not None and limit >= 0:
            return out[-int(limit):]
        return out

    def tail(self, limit: int = 50, **filters: Any) -> list[dict[str, Any]]:
        """Return the last ``limit`` matching events."""
        return self.events(limit=limit, **filters)


def _event_matches(
    event: dict[str, Any],
    *,
    kind: str | None,
    command_id: str | None,
    order_id: str | None,
    reference: str | None,
) -> bool:
    if kind is not None and event.get("kind") != kind:
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if command_id is not None:
        if command_id not in {event.get("command_id"), payload.get("command_id"), payload.get("id")}:
            return False
    if order_id is not None:
        if order_id not in {event.get("order_id"), payload.get("order_id")}:
            return False
    if reference is not None:
        req = payload.get("req") if isinstance(payload.get("req"), dict) else {}
        request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
        refs = {
            event.get("reference"),
            payload.get("reference"),
            req.get("reference"),
            request.get("reference"),
        }
        if reference not in refs:
            return False
    return True


def _event_time(raw: Any, *, fallback: datetime) -> datetime:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return fallback
