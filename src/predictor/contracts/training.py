"""Causal training-input reader; no fitting, optimizer or model implementation."""
from predictor.contracts.forecast import LinkageError, timestamp


def training_labels(journal, forecast_ids, *, fit_started_at):
    """Return only recorded, mature labels strictly before the fit boundary.

    A latest unresolved correction blocks reuse of an older mature label through
    RecordJournal.mature_label. Missing/not-yet-observed rows fail closed.
    """
    fit = timestamp(fit_started_at)
    if not isinstance(forecast_ids, (list, tuple)) or not forecast_ids or any(not isinstance(i, str) or not i.strip() for i in forecast_ids) or len(set(forecast_ids)) != len(forecast_ids):
        raise LinkageError("Training requires unique forecast identifiers")
    labels = []
    for forecast_id in forecast_ids:
        label = journal.mature_label(forecast_id, at_time=fit)
        value = label.record()
        if max(timestamp(value["label_mature_at"]), timestamp(value["recorded_at"])) >= fit:
            raise LinkageError("Training labels must be recorded and mature strictly before fit_started_at")
        labels.append(value)
    return tuple(labels)
