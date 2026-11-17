"""Thin wrappers preserve original NYSE/availability/quality evidence boundaries."""
from predictor.data.adapters.prices import parse_response as parse_nyse_prices, replay_prices
from predictor.data.adapters.reference import parse_reference as parse_nyse_reference, replay_references
from predictor.contracts.snapshot import DevelopmentRecords
from predictor.access import AccessPolicy


def nyse_price_replay(policy, directory, **kwargs):
    return replay_prices(policy, directory, **kwargs)


def nyse_reference_replay(policy, directory, **kwargs):
    return replay_references(policy, directory, **kwargs)
