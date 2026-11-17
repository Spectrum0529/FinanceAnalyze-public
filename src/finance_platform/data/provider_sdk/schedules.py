"""Data-specific plan facade over the single shared SchedulerService."""
from finance_platform.data.contracts import DataError, validate_paging
from finance_platform.data.jobs import connector_for


class CollectionSchedules:
    def __init__(self, service, runtime):
        self.service, self.runtime = service, runtime

    def create(self, principal, *, dataset_id, start_at, interval_seconds, parameters, max_pages=100):
        d = self.service.dataset(principal.user_id, dataset_id)
        s = self.service.source(principal.user_id, d["source_id"], d["source_version"])
        sdk=s["provider"] in {"yfinance","akshare"}
        if sdk:
            self.service.resolve_call(principal.user_id,dataset_id,parameters)
            if max_pages!=1:raise DataError("SDK_UNIT_LIMIT")
        else:connector_for(s).initial(parameters)
        value = {"name": "数据采集", "handler": "data.collect", "mode": "BACKTEST", "rule": "INTERVAL",
                 "interval_seconds": interval_seconds, "start_at": start_at,
                 "parameters": {"dataset_id": dataset_id, "parameters": parameters, "collection_id": None, "max_pages": max_pages}}
        schedule = self.runtime.scheduler.create(principal, value)
        if not sdk and connector_for(s).config.missing():
            schedule = self.runtime.scheduler.set_paused(principal, schedule["schedule_id"], True)
        return schedule

    def list(self, principal, limit=50, offset=0):
        validate_paging(limit, offset)
        result = self.runtime.scheduler.list(principal, limit=limit, offset=offset)
        result["items"] = [s for s in result["items"] if s["handler"] == "data.collect"]
        result["total_scope"] = "ALL_SCHEDULES_BEFORE_DATA_FILTER"
        return result

    def set_enabled(self, principal, schedule_id, enabled):
        if type(enabled) is not bool:
            raise DataError("SCHEDULE_FLAG_INVALID")
        s = self.runtime.scheduler.get(principal, schedule_id)
        if s["handler"] != "data.collect":
            raise DataError("RESOURCE_NOT_FOUND")
        if enabled:
            d = self.service.dataset(s["owner_id"], s["parameters"]["dataset_id"])
            source = self.service.source(s["owner_id"], d["source_id"], d["source_version"])
            if source["provider"] in {"yfinance","akshare"}:
                self.service.resolve_call(s["owner_id"],s["parameters"]["dataset_id"],s["parameters"]["parameters"])
            elif connector_for(source).config.missing():
                raise DataError("CONNECTION_NOT_CONFIGURED")
        return self.runtime.scheduler.set_paused(principal, schedule_id, not enabled)
