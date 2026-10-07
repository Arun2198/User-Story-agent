"""Drift: online metrics compared with an offline baseline."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from story_agent.evals.online.config import DriftConfig, Tolerance


@dataclass(frozen=True)
class Drift:
    """One metric that moved further than allowed."""

    metric: str
    online: float
    baseline: float
    allowed: float

    def __str__(self) -> str:
        """Describe the drift in one line."""
        return (
            f"{self.metric}: online {self.online:.4g}, baseline {self.baseline:.4g}, "
            f"allowed +/-{self.allowed:.4g}"
        )


@dataclass(frozen=True)
class DriftReport:
    """Result of a drift check."""

    runs: int
    checked: tuple[str, ...]
    drifted: tuple[Drift, ...]
    skipped: str = ""

    @property
    def ok(self) -> bool:
        """True when nothing drifted (or the check was skipped for too few runs)."""
        return not self.drifted


def mean_metrics(rows: Sequence[Mapping[str, float]]) -> dict[str, float]:
    """Average each metric over the runs that report it."""
    keys = sorted({k for row in rows for k in row})
    return {
        k: sum(row[k] for row in rows if k in row) / sum(1 for row in rows if k in row)
        for k in keys
    }


def _allowed(tolerance: Tolerance, baseline: float) -> float:
    steps = []
    if tolerance.abs is not None:
        steps.append(tolerance.abs)
    if tolerance.rel is not None:
        steps.append(abs(baseline) * tolerance.rel)
    return max(steps) if steps else 0.0


def check_drift(
    rows: Sequence[Mapping[str, float]], baseline: Mapping[str, float], config: DriftConfig
) -> DriftReport:
    """Compare the mean of ``rows`` with ``baseline`` for every metric that has a tolerance."""
    if len(rows) < config.min_runs:
        return DriftReport(
            len(rows), (), (), f"{len(rows)} runs; at least {config.min_runs} are needed"
        )
    online = mean_metrics(rows)
    checked: list[str] = []
    drifted: list[Drift] = []
    for metric, tolerance in config.tolerances.items():
        if metric not in online or metric not in baseline:
            continue
        checked.append(metric)
        allowed = _allowed(tolerance, baseline[metric])
        if abs(online[metric] - baseline[metric]) > allowed:
            drifted.append(Drift(metric, online[metric], baseline[metric], allowed))
    return DriftReport(len(rows), tuple(checked), tuple(drifted))
