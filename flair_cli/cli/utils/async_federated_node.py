"""Standalone local Flower aggregation node used by the merge command."""

from typing import List, Tuple
from uuid import uuid4

from flwr.common import Code, FitRes, Parameters, Status
from flwr.server.client_proxy import ClientProxy
from flwr.server.strategy import Strategy

from .aggregatable import Aggregatable


class AsyncFederatedNode:
    """Run Flower strategy aggregation without the flwr-serverless package."""

    def __init__(
        self,
        shared_folder,
        strategy: Strategy,
        ignore_seen_models: bool = False,
        node_id: str | None = None,
    ):
        self.node_id = node_id or str(uuid4())
        self.counter = 0
        self.strategy = strategy
        self.model_store = shared_folder
        self.ignore_seen_models = ignore_seen_models
        self.seen_models = set()

    def _aggregate(self, aggregatables: List[Aggregatable]) -> Aggregatable:
        if not aggregatables:
            raise ValueError("No aggregatables supplied")

        results: List[Tuple[ClientProxy, FitRes]] = [
            (
                None,
                FitRes(
                    status=Status(code=Code.OK, message="Success"),
                    parameters=item.parameters,
                    num_examples=item.num_examples,
                    metrics=item.metrics or {},
                ),
            )
            for item in aggregatables
        ]

        aggregated_parameters, aggregated_metrics = self.strategy.aggregate_fit(
            server_round=self.counter + 1,
            results=results,
            failures=[],
        )
        self.counter += 1

        if aggregated_parameters is None:
            raise ValueError("Flower strategy did not produce aggregated parameters")

        return Aggregatable(
            parameters=aggregated_parameters,
            num_examples=sum(item.num_examples for item in aggregatables),
            metrics=aggregated_metrics or {},
        )