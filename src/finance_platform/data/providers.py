"""Concrete Alpaca raw bars and SEC EDGAR schemas, no live trading calls."""
from datetime import timedelta, datetime, timezone, date
from decimal import Decimal
import re
from urllib.parse import urlencode

from finance_platform.core.contracts import parse_instant, payload, fingerprint
from .contracts import DataError, load_json, bar_values, identities_at, envelope, number
from .network import SafeHTTP, HOSTS, ConnectionConfig


def schema_checked(function):
    from functools import wraps
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DataError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise DataError("PROVIDER_RESPONSE_SCHEMA_INVALID") from None
    return checked


def sec_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise DataError("SEC_DATE_INVALID")
    day = date.fromisoformat(value)
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


class AlpacaBars:
    contract_version = "platform-data-1"
    source_schema = "alpaca-stock-bars-v2"
    interval_seconds = 0.31  # below Basic's documented 200/min; account-wide budget still applies.

    def __init__(self, connection):
        self.config = ConnectionConfig.from_mapping(connection, "alpaca")

    def transport(self):
        return SafeHTTP(HOSTS["alpaca"], ("/v2/stocks/bars",), allow_page_token=True)

    def initial(self, parameters):
        if set(parameters) != {"symbols", "start", "end", "timeframe", "limit"}:
            raise DataError("ALPACA_PARAMETERS_INVALID")
        if parameters["timeframe"] not in {"1Day", "1Min"} or type(parameters["limit"]) is not int or not 1 <= parameters["limit"] <= 10000:
            raise DataError("ALPACA_PARAMETERS_INVALID")
        if not isinstance(parameters["symbols"], list) or not 1 <= len(parameters["symbols"]) <= 100 or len(set(parameters["symbols"])) != len(parameters["symbols"]) or any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,17}", s) for s in parameters["symbols"]):
            raise DataError("ALPACA_SYMBOLS_INVALID")
        if parse_instant(parameters["start"]) > parse_instant(parameters["end"]):
            raise DataError("TIME_RANGE_INVALID")
        return {**parameters, "symbols": ",".join(parameters["symbols"]), "feed": self.config.feed,
                "adjustment": "raw", "currency": "USD", "sort": "asc"}

    def url(self, cursor):
        return (self.config.endpoint or "https://data.alpaca.markets") + "/v2/stocks/bars?" + urlencode(cursor)

    @schema_checked
    def parse(self, raw, *, cursor, dataset, license_id, received_at):
        data = load_json(raw)
        if not isinstance(data, dict) or "next_page_token" not in data or not isinstance(data.get("bars"), dict):
            raise DataError("ALPACA_FIELDS_MISSING")
        records = []
        for symbol, bars in data["bars"].items():
            if symbol not in cursor["symbols"].split(",") or not isinstance(bars, list):
                raise DataError("UNREQUESTED_SYMBOL")
            for bar in bars:
                if not {"t", "o", "h", "l", "c", "v"} <= bar.keys():
                    raise DataError("ALPACA_FIELDS_MISSING")
                effective = parse_instant(bar["t"])
                if cursor["timeframe"] == "1Min" and (effective.second or effective.microsecond):
                    raise DataError("BAR_START_NOT_MINUTE_ALIGNED")
                if not parse_instant(cursor["start"]) <= effective <= parse_instant(cursor["end"]):
                    raise DataError("BAR_OUTSIDE_REQUEST")
                instrument = identities_at(dataset["identities"], symbol, bar["t"])
                row = {k: bar[v] for k, v in {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"}.items()}
                row.update(frequency=cursor["timeframe"], trading_status="TRADABLE" if instrument.tradable else "HALTED")
                values = bar_values(row)
                end = effective + (timedelta(minutes=1) if cursor["timeframe"] == "1Min" else timedelta(days=1))
                values.update(bar_end=payload(end), feed=cursor["feed"], source_timestamp=bar["t"],
                              historical_first_available="UNKNOWN_OBSERVED_RESPONSE_ONLY")
                if "vw" in bar and bar["vw"] is not None:
                    values["vwap"] = number(bar["vw"], minimum=Decimal(0))
                if "n" in bar:
                    if type(bar["n"]) is not int or bar["n"] < 0:
                        raise DataError("TRADE_COUNT_INVALID")
                    values["trade_count"] = bar["n"]
                records.append(envelope(kind="bar", source_id=dataset["source_id"], license_id=license_id,
                    scope=dataset["scope"], instrument_id=instrument.instrument_id, effective_at=payload(effective),
                    available_at=payload(max(end, parse_instant(received_at))), received_at=received_at,
                    values=values, natural_key=[instrument.instrument_id, payload(effective), cursor["timeframe"], cursor["feed"]]))
        if len(records) > cursor["limit"]:
            raise DataError("PROVIDER_PAGE_LIMIT_EXCEEDED")
        token = data.get("next_page_token")
        if token is not None and (not isinstance(token, str) or not token or len(token) > 2048):
            raise DataError("PAGE_TOKEN_INVALID")
        return records, None if token is None else {**cursor, "page_token": token}


class SECEdgar:
    contract_version = "platform-data-1"
    source_schema = "sec-edgar-json-v1"
    interval_seconds = 0.2  # 5/s per collector, below SEC 10/s total user/machine aggregate.

    def __init__(self, connection):
        self.config = ConnectionConfig.from_mapping(connection, "sec")

    def transport(self):
        return SafeHTTP(HOSTS["sec"], ("/submissions/", "/api/xbrl/companyfacts/"))

    def initial(self, parameters):
        if set(parameters) != {"cik", "kind"} or parameters["kind"] not in {"submissions", "companyfacts"} or not isinstance(parameters["cik"], str) or not re.fullmatch(r"[0-9]{1,10}", parameters["cik"]) or int(parameters["cik"]) == 0:
            raise DataError("SEC_PARAMETERS_INVALID")
        cik = parameters["cik"].zfill(10)
        return {"cik": cik, "kind": parameters["kind"], "file": "CIK" + cik + ".json", "pending": []}

    def url(self, cursor):
        file = cursor["file"]
        if not re.fullmatch(r"CIK[0-9]{10}(?:-submissions-[0-9]{3})?\.json", file):
            raise DataError("SEC_FILE_INVALID")
        prefix = "/submissions/" if cursor["kind"] == "submissions" else "/api/xbrl/companyfacts/"
        return (self.config.endpoint or "https://data.sec.gov") + prefix + file

    @schema_checked
    def parse(self, raw, *, cursor, dataset, license_id, received_at):
        data = load_json(raw)
        issuer = "sec-cik:" + cursor["cik"]
        records, pending = [], list(cursor["pending"])
        if cursor["kind"] == "submissions":
            if "filings" in data:
                if str(data.get("cik")).zfill(10) != cursor["cik"]:
                    raise DataError("SEC_CIK_MISMATCH")
                rows = data["filings"].get("recent", {})
                for file in data["filings"].get("files", []):
                    name = file.get("name", "")
                    if not re.fullmatch(r"CIK" + cursor["cik"] + r"-submissions-[0-9]{3}\.json", name):
                        raise DataError("SEC_FILE_INVALID")
                    pending.append(name)
            else:
                rows = data
            required = {"accessionNumber", "filingDate", "reportDate", "form", "primaryDocument"}
            if not required <= rows.keys() or any(not isinstance(v, list) for v in rows.values()) or len({len(v) for v in rows.values()}) != 1:
                raise DataError("SEC_COLUMN_ARRAYS_INVALID")
            for i in range(len(rows["accessionNumber"])):
                values = {k: v[i] for k, v in rows.items()}
                accession = values["accessionNumber"]
                if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
                    raise DataError("SEC_ACCESSION_INVALID")
                try:
                    effective = sec_date(values["filingDate"])
                except ValueError:
                    raise DataError("SEC_FILING_DATE_INVALID") from None
                values.update(issuer_id=issuer, timestamp_basis="OBSERVED_ONLY_FILING_DATE_NOT_HISTORICAL_AVAILABILITY")
                records.append(envelope(kind="filing", source_id=dataset["source_id"], license_id=license_id,
                    scope=dataset["scope"], instrument_id=issuer, effective_at=payload(effective), available_at=received_at,
                    received_at=received_at, values=values, natural_key=accession))
        else:
            if str(data.get("cik")).zfill(10) != cursor["cik"] or not isinstance(data.get("facts"), dict):
                raise DataError("SEC_FACTS_SCHEMA_INVALID")
            for taxonomy, concepts in data["facts"].items():
                for tag, concept in concepts.items():
                    if not isinstance(concept.get("units"), dict):
                        raise DataError("SEC_FACTS_SCHEMA_INVALID")
                    for unit, facts in concept["units"].items():
                        for fact in facts:
                            if not {"end", "val", "accn", "filed", "form"} <= fact.keys():
                                raise DataError("SEC_FACT_FIELDS_MISSING")
                            effective = sec_date(fact["end"])
                            sec_date(fact["filed"])
                            if fact.get("start"):
                                sec_date(fact["start"])
                            if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", fact["accn"]):
                                raise DataError("SEC_ACCESSION_INVALID")
                            values = {**fact, "val": number(fact["val"]), "unit": unit, "taxonomy": taxonomy, "tag": tag, "issuer_id": issuer,
                                      "timestamp_basis": "OBSERVED_ONLY_FILED_DATE_NOT_HISTORICAL_AVAILABILITY"}
                            records.append(envelope(kind="fact", source_id=dataset["source_id"], license_id=license_id,
                                scope=dataset["scope"], instrument_id=issuer, effective_at=payload(effective), available_at=received_at,
                                received_at=received_at, values=values,
                                natural_key=[taxonomy, tag, unit, fact["end"], fact.get("start"), fact["accn"], fact.get("frame")]))
        return records, {**cursor, "file": pending[0], "pending": pending[1:]} if pending else None


class RestJSON:
    """Limited JSON rows + opaque token protocol; no arbitrary URL next-links."""
    interval_seconds = 1
    source_schema = "rest-json-rows-v1"

    def __init__(self, connection, *, allowed_hosts, path_prefixes):
        self.config = ConnectionConfig.from_mapping(connection, "rest")
        self.allowed_hosts, self.path_prefixes = allowed_hosts, path_prefixes

    def transport(self):
        return SafeHTTP(self.allowed_hosts, self.path_prefixes, allow_page_token=True)

    def initial(self, parameters):
        if set(parameters) != {"query"} or not isinstance(parameters["query"], dict):
            raise DataError("REST_PARAMETERS_INVALID")
        return dict(parameters["query"])

    def url(self, cursor):
        from .network import validate_url
        url = self.config.endpoint + ("&" if "?" in self.config.endpoint else "?") + urlencode(cursor)
        validate_url(url, self.allowed_hosts, path_prefixes=self.path_prefixes, allow_page_token=True)
        return url

    @schema_checked
    def parse(self, raw, *, cursor, dataset, license_id, received_at):
        from .contracts import validate_record
        data = load_json(raw)
        if set(data) != {"records", "next_page_token"} or not isinstance(data["records"], list):
            raise DataError("REST_SCHEMA_INVALID")
        records = []
        for r in data["records"]:
            validate_record(r)
            r["metadata"]["received_at"] = received_at
            r["metadata"]["available_at"] = payload(max(parse_instant(received_at), parse_instant(r["metadata"]["available_at"])))
            records.append(r)
        token = data["next_page_token"]
        if token is not None and (not isinstance(token, str) or len(token) > 2048):
            raise DataError("PAGE_TOKEN_INVALID")
        return records, None if token is None else {**cursor, "page_token": token}
