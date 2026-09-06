"""Flower parameters and aggregation metadata used by the local merge node."""

from dataclasses import dataclass
from typing import Any

from flwr.common import Parameters


@dataclass
class Aggregatable:
    parameters: Parameters
    num_examples: int
    metrics: dict[str, Any] | None = None