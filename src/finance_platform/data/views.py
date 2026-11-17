"""Bound owner/dataset views for trusted research adapters; values to strategies."""
from finance_platform.core.contracts import payload, freeze
from . import CONTRACT_VERSION


class DatasetView:
    contract_version = CONTRACT_VERSION

    def __init__(self, service, owner_id, dataset_id, *, expected_bars=(), max_delay_seconds=None):
        self.service, self.owner_id, self.dataset_id = service, owner_id, dataset_id
        self.expected_bars, self.max_delay_seconds = expected_bars, max_delay_seconds

    def snapshot(self, *, as_of, instrument_ids):
        result = self.service.snapshot(self.owner_id, self.dataset_id, as_of=payload(as_of), instrument_ids=instrument_ids,
                                       expected_bars=self.expected_bars, max_delay_seconds=self.max_delay_seconds)
        checked = self.service.get_snapshot(self.owner_id, result["snapshot_id"], require_quality=True)
        # Installed research adapters may use this service; StrategyContext receives only this value copy.
        return freeze(checked)
