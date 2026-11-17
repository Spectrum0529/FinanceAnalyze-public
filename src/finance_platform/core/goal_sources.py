"""Project registered data provider capabilities for goal admission."""
from finance_platform.core.errors import PlatformError
from finance_platform.data.goal_data.goal_data import CAPABILITIES
from finance_platform.data.goal_data.goal_sdk import TUSHARE_TLS


class GoalSources:
    def __init__(self, library, credential_store, runners, *, existing_sources=()):
        self.library = library
        self.credential_store = credential_store
        self.runners = runners
        self.existing_sources = frozenset(existing_sources)

    def _configured(self, owner, provider, reference):
        if not reference:
            return False
        try:
            info = self.credential_store.binding_for_reference(owner, reference)
        except PlatformError as error:
            if error.code == "CREDENTIAL_NOT_CONFIGURED":
                return False
            raise
        expected = {"tushare": TUSHARE_TLS, "massive": "https://api.massive.com"}[provider]
        return (info["configured"] and info["provider"] == provider and info["endpoint_id"] is None
                and info["base_url"] == expected)

    def _connection(self, owner, connection_id, record):
        provider = record["provider"]
        capability = CAPABILITIES.get(provider)
        if capability is None:
            return None
        runner = self.runners.get(provider)
        configured = not capability.credentials or runner is not None and runner.fixture is not None
        if not configured:
            configured = self._configured(owner, provider, record.get("credential_ref"))
        bound = runner is not None or provider in self.existing_sources
        execution = "BOUND" if bound and configured else (
            "CREDENTIAL_REQUIRED" if not configured else "REUSE_ONLY"
        )
        rows = [{"market": capability.market, "asset": asset, "kind": "bars", "frequency": "1Day",
                 "adjustment": adjustment, "currency": capability.currency}
                for asset in capability.assets for adjustment in capability.adjustments]
        return {"connection_id": connection_id, "provider": provider, "execution": execution,
                "scope": "synthetic" if runner is not None and runner.fixture is not None else "public",
                "capabilities": rows}

    def options(self, principal, connection_ids):
        sources = []
        for connection_id in connection_ids:
            try:
                record = self.library.connection(principal.user_id, connection_id)
            except PlatformError as error:
                if error.code == "RESOURCE_NOT_FOUND":
                    raise PlatformError("GOAL_SOURCE_REQUIRED", 409) from error
                raise
            item = self._connection(principal.user_id, connection_id, record)
            if item is not None:
                sources.append(item)
        reference = next((source for source in sources if source["provider"] == "tushare"
                          and source["execution"] == "BOUND"), None)
        if reference:
            for source in sources:
                if source["provider"] == "akshare_cn" and source["scope"] == reference["scope"]:
                    source["reference_connection_id"] = reference["connection_id"]
        return [source for source in sources if source["execution"] != "CREDENTIAL_REQUIRED"]

    def catalog(self, owner):
        connections = self.library.list_connections(owner)["items"]
        result = []
        for provider in ("akshare_cn", "tushare", "openbb_nasdaq"):
            cap = CAPABILITIES[provider]
            runner = self.runners.get(provider)
            configured = False
            if cap.credentials and runner is not None:
                # Multiple saved connections may name the same vault slot.
                # Resolve each reference once for this owner and catalog read.
                references = dict.fromkeys(
                    row.get("credential_ref") for row in connections if row["provider"] == provider
                )
                configured = any(self._configured(owner, provider, ref) for ref in references)
            result.append(cap.view(runtime_ready=runner is not None, credential_configured=configured))
        return result
