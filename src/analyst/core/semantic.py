"""Loads the semantic layer (metrics.yaml) and access rules (roles.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from config.settings import settings


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    description: str
    format: str
    higher_is_better: bool
    expression: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    synonyms: tuple[str, ...] = ()

    @property
    def is_ratio(self) -> bool:
        return self.numerator is not None and self.denominator is not None

    def sql(self) -> str:
        """SQL aggregate expression for this metric (expects base relation aliased `t`)."""
        if self.is_ratio:
            return f"CAST({self.numerator} AS DOUBLE) / NULLIF({self.denominator}, 0)"
        assert self.expression is not None
        return self.expression


@dataclass(frozen=True)
class Dimension:
    name: str
    sql: str
    join: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class SemanticLayer:
    base_sql: str
    time_column: str
    metrics: dict[str, Metric]
    dimensions: dict[str, Dimension]
    joins: dict[str, str]
    time_grains: dict[str, str]
    allowed_tables: dict[str, tuple[str, ...]]

    def metric(self, name: str) -> Metric:
        if name not in self.metrics:
            raise KeyError(f"Unknown metric '{name}'. Known: {sorted(self.metrics)}")
        return self.metrics[name]

    def dimension(self, name: str) -> Dimension:
        if name not in self.dimensions:
            raise KeyError(f"Unknown dimension '{name}'. Known: {sorted(self.dimensions)}")
        return self.dimensions[name]

    def describe(self) -> str:
        """Compact description used in LLM prompts."""
        lines = ["METRICS:"]
        for m in self.metrics.values():
            lines.append(f"- {m.name}: {m.label}. {m.description}".rstrip())
        lines.append("DIMENSIONS:")
        for d in self.dimensions.values():
            note = f" ({d.note})" if d.note else ""
            lines.append(f"- {d.name}{note}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    allowed_tables: frozenset[str]
    blocked_columns: dict[str, frozenset[str]] = field(default_factory=dict)
    scope: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def all_blocked_columns(self) -> frozenset[str]:
        out: set[str] = set()
        for cols in self.blocked_columns.values():
            out |= cols
        return frozenset(out)


@dataclass(frozen=True)
class User:
    username: str
    display: str
    role: Role


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=4)
def load_semantic_layer(path: Path | None = None) -> SemanticLayer:
    raw = _read_yaml(path or settings.metrics_path)
    metrics = {
        name: Metric(
            name=name,
            label=spec["label"],
            description=spec.get("description", ""),
            format=spec.get("format", "number"),
            higher_is_better=spec.get("higher_is_better", True),
            expression=spec.get("expression"),
            numerator=spec.get("numerator"),
            denominator=spec.get("denominator"),
            synonyms=tuple(spec.get("synonyms", [])),
        )
        for name, spec in raw["metrics"].items()
    }
    dimensions = {
        name: Dimension(name=name, sql=spec["sql"], join=spec.get("join"), note=spec.get("note"))
        for name, spec in raw["dimensions"].items()
    }
    return SemanticLayer(
        base_sql=raw["base_relation"]["sql"],
        time_column=raw["base_relation"]["time_column"],
        metrics=metrics,
        dimensions=dimensions,
        joins=raw["joins"],
        time_grains=raw["time_grains"],
        allowed_tables={t: tuple(cols) for t, cols in raw["allowed_tables"].items()},
    )


@lru_cache(maxsize=4)
def load_roles(path: Path | None = None) -> tuple[dict[str, Role], dict[str, User]]:
    raw = _read_yaml(path or settings.roles_path)
    roles = {
        name: Role(
            name=name,
            description=spec.get("description", ""),
            allowed_tables=frozenset(spec["allowed_tables"]),
            blocked_columns={
                t: frozenset(c) for t, c in (spec.get("blocked_columns") or {}).items()
            },
            scope={k: tuple(v) for k, v in (spec.get("scope") or {}).items()},
        )
        for name, spec in raw["roles"].items()
    }
    users = {
        uname: User(username=uname, display=spec["display"], role=roles[spec["role"]])
        for uname, spec in raw["users"].items()
    }
    return roles, users


def get_user(username: str) -> User:
    _, users = load_roles()
    if username not in users:
        raise KeyError(f"Unknown user '{username}'")
    return users[username]
