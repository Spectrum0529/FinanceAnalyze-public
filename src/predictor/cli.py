"""Engineering contracts and guarded development sample acquisition; no model."""

import argparse
import json
from pathlib import Path
import sys

from predictor import __version__
from predictor.config import load_config
from predictor.logging import configure_logging, utc_timestamp
from predictor.research.ledger import Ledger, LedgerError
import sqlite3
import hashlib
from predictor.access import AccessPolicy
from predictor.secrets import redact
from predictor.contracts.protocol import load_protocol, readiness, require_action


def ledger_command(args) -> int:
    ledger = Ledger(args.root)
    if args.action == "init":
        ledger.initialize(args.source)
    elif args.action == "import-namespace":
        ledger.import_namespace(args.source, namespace=args.namespace,
            provenance=json.loads(args.provenance.read_text(encoding="utf-8")))
    elif args.action == "task":
        evidence = json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence else None
        dimensions = json.loads(args.dimensions) if args.dimensions else None
        ledger.transition_task(args.task_id, args.status, reason=args.reason,
                               evidence=evidence, dimensions=dimensions)
    elif args.action == "start-run":
        ledger.start_run(json.loads(args.metadata.read_text(encoding="utf-8")))
    elif args.action == "finish-run":
        ledger.finish_run(args.run_id, args.status, json.loads(args.result.read_text(encoding="utf-8")))
    elif args.action == "show-run":
        print(json.dumps(ledger.get_run(args.run_id), ensure_ascii=False))
        return 0
    elif args.action == "decision":
        ledger.append_decision(args.decision_id, json.loads(args.payload.read_text(encoding="utf-8")))
    plan = ledger.export_views()
    print(json.dumps({"status": "ok", "action": args.action,
                      "tasks": [{"id": t["id"], "status": t["status"],
                                 "status_dimensions": t["status_dimensions"]} for t in plan["tasks"]]}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="predictor")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Check the engineering shell")
    check.add_argument("--config", type=Path, help="Explicit UTF-8 JSON config path")
    check.add_argument("--root", type=Path, help="Repository root for config access; defaults to cwd")
    protocol = commands.add_parser("protocol-check", help="Validate the working research protocol")
    protocol.add_argument("--root", type=Path, required=True)
    protocol.add_argument("--config", type=Path, default=Path("configs/protocol.json"))
    protocol.add_argument("--require", dest="release_level", choices=[
        "synthetic_engineering", "direction_prediction", "economic_evaluation", "paper_trading", "live_orders"])
    calendar = commands.add_parser("calendar-check", help="Inspect the locked exchange forecast clock")
    calendar.add_argument("--root", type=Path, required=True)
    calendar.add_argument("--config", type=Path, default=Path("configs/protocol.calendar.json"))
    calendar.add_argument("--calendar-lock", type=Path, default=Path("configs/calendar.lock.json"))
    calendar.add_argument("--session", required=True)
    calendar.add_argument("--horizon", type=int, default=5)
    record = commands.add_parser("record-check", help="Inspect guarded raw-record availability metadata")
    record.add_argument("--root", type=Path, required=True)
    record.add_argument("--path", required=True)
    record.add_argument("--cutoff", required=True, help="Explicit timezone-aware ISO timestamp")
    record.add_argument("--event-reference", required=True, help="YYYY-MM-DD or timezone-aware ISO timestamp")
    linkage = commands.add_parser("linkage-check", help="Replay record interfaces in an isolated development journal")
    linkage.add_argument("--root", type=Path, required=True)
    for field in ("forecast", "label", "evaluation"):
        linkage.add_argument("--" + field, required=True)
    split = commands.add_parser("split-check", help="Inspect guarded split policy and authoritative window register")
    split.add_argument("--root", type=Path, required=True)
    split.add_argument("--config", default="configs/split_policy.json")
    split.add_argument("--require-final", action="store_true")
    synthetic = commands.add_parser("synthetic-check", help="Replay a guarded empty-model engineering fixture")
    synthetic.add_argument("--root", type=Path, required=True)
    synthetic.add_argument("--path", required=True)
    prices = commands.add_parser("price-replay", help="Replay a guarded NYSE daily-summary capture; auctions unavailable")
    prices.add_argument("--root", type=Path, required=True)
    prices.add_argument("--capture", required=True)
    prices.add_argument("--mapping", help="Optional guarded dated mapping JSON")
    fetch = commands.add_parser("price-fetch-sample", help="Capture one bounded official NYSE public daily sample")
    fetch.add_argument("--root", type=Path, required=True)
    fetch.add_argument("--capture", required=True)
    fetch.add_argument("--session", required=True)
    reference = commands.add_parser('reference-replay', help='Guarded NYSE identity/action evaluation capture replay')
    reference.add_argument('--root', type=Path, required=True)
    reference.add_argument('--capture', required=True)
    raw_import = commands.add_parser('raw-import-capture', help='Index an existing guarded sample capture without downloading')
    raw_import.add_argument('--root', type=Path, required=True)
    raw_import.add_argument('--store', required=True)
    raw_import.add_argument('--capture', required=True)
    raw_import.add_argument('--kind', choices=['prices', 'reference'], required=True)
    raw_import.add_argument('--batch-id', required=True)
    raw_manifest = commands.add_parser('raw-manifest', help='Validate development raw objects and append-only index')
    raw_manifest.add_argument('--root', type=Path, required=True)
    raw_manifest.add_argument('--store', required=True)
    raw_replay = commands.add_parser('raw-replay', help='Verify one indexed raw version, printing only hash and size')
    raw_replay.add_argument('--root', type=Path, required=True)
    raw_replay.add_argument('--store', required=True)
    raw_replay.add_argument('--request-key', required=True)
    raw_replay.add_argument('--version', type=int, required=True)
    for name in ("dev-check", "data-read"):
        sub = commands.add_parser(name, help="Guarded development access")
        sub.add_argument("--root", type=Path, required=True)
        sub.add_argument("--policy", type=Path)
        if name == "dev-check":
            sub.add_argument("--config", type=Path)
        else:
            sub.add_argument("--path", required=True)
    ledger_parser = commands.add_parser("ledger", help="Task and experiment ledger")
    actions = ledger_parser.add_subparsers(dest="action", required=True)
    for action in ("init", "import-namespace", "tasks", "task", "start-run", "finish-run", "show-run", "decision"):
        sub = actions.add_parser(action)
        sub.add_argument("--root", type=Path, required=True, help="Explicit repository root")
        if action == "init":
            sub.add_argument("--source", type=Path, required=True)
        elif action == "import-namespace":
            sub.add_argument("--source", type=Path, required=True)
            sub.add_argument("--namespace", required=True)
            sub.add_argument("--provenance", type=Path, required=True)
        elif action == "task":
            sub.add_argument("--task-id", required=True)
            sub.add_argument("--status", choices=["not_started", "in_progress", "blocked", "done", "deferred"], required=True)
            sub.add_argument("--reason", required=True)
            sub.add_argument("--evidence", type=Path)
            sub.add_argument("--dimensions", help="JSON object of independent status dimensions")
        elif action == "start-run":
            sub.add_argument("--metadata", type=Path, required=True)
        elif action == "finish-run":
            sub.add_argument("--run-id", required=True)
            sub.add_argument("--status", choices=["succeeded", "failed", "interrupted"], required=True)
            sub.add_argument("--result", type=Path, required=True)
        elif action == "show-run":
            sub.add_argument("--run-id", required=True)
        elif action == "decision":
            sub.add_argument("--decision-id", required=True)
            sub.add_argument("--payload", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "ledger":
            return ledger_command(args)
        root = args.root or Path.cwd()
        policy = AccessPolicy(root, getattr(args, "policy", None))
        if args.command == 'raw-import-capture':
            from predictor.data.manifest import ingest_capture
            print(json.dumps({'status': 'ok', **ingest_capture(policy, args.store, args.capture, args.kind, args.batch_id)}, ensure_ascii=False))
            return 0
        if args.command in {'raw-manifest', 'raw-replay'}:
            from predictor.data.raw_store import RawStore, canonical, digest
            store = RawStore(policy, args.store)
            if args.command == 'raw-manifest':
                inventory = store.manifest()
                print(json.dumps({'status': 'ok', 'counts': inventory['counts'],
                    'manifest_hash': digest(canonical(inventory).encode('utf-8')), 'data_certification': 'UNVERIFIED',
                    'formal_research_eligible': False, 'model_evaluation': 'NOT_RUN'}, ensure_ascii=False))
            else:
                body = store.read_version(args.request_key, args.version)
                print(json.dumps({'status': 'ok', 'body_hash': digest(body), 'byte_count': len(body)}, ensure_ascii=False))
            return 0
        if args.command == 'reference-replay':
            from predictor.data.adapters.reference import replay_references
            from predictor.contracts.snapshot import content_hash
            result = replay_references(policy, args.capture)
            print(json.dumps({**{k:v for k,v in result.items() if k != 'records'},
                'normalized_hash': content_hash(result['records'])}, ensure_ascii=False))
            return 0
        if args.command == "price-fetch-sample":
            from datetime import date
            from predictor.data.acquisition import download_sample
            manifest = download_sample(policy, args.capture, date.fromisoformat(args.session))
            print(json.dumps({"status": "ok", "scope": "public_provider_sample_evaluation", "capture_manifest": manifest,
                              "formal_label_eligible": False, "data_certification": "UNVERIFIED", "model_evaluation": "NOT_RUN"}, ensure_ascii=False))
            return 0
        if args.command == "price-replay":
            from predictor.data.adapters.prices import replay_prices
            from predictor.contracts.snapshot import content_hash
            mapping = json.loads(policy.authorize(args.mapping, operation="config_read").read_bytes()) if args.mapping else []
            result = replay_prices(policy, args.capture, mappings=mapping)
            summary = {key: value for key, value in result.items() if key != "records"}
            print(json.dumps({"status": "ok", **summary, "normalized_payload_hash": content_hash(
                [r["payload"] for r in result["records"]])}, ensure_ascii=False))
            return 0
        if args.command == "synthetic-check":
            from predictor.engineering.synthetic import replay
            print(json.dumps({"status": "ok", **replay(root, args.path)}, ensure_ascii=False))
            return 0
        if args.command == "split-check":
            from predictor.research.windows import WindowRegister, load_policy, WindowError
            result = WindowRegister(root, policy=load_policy(root, args.config)).status()
            if args.require_final:
                raise WindowError("Final evaluation blocked: concrete release lock and final-run binding required")
            print(json.dumps({"status": "ok", "scope": "split_access_contract_only", **result}, ensure_ascii=False))
            return 0
        if args.command == "linkage-check":
            import tempfile
            from predictor.contracts.protocol import parse_json
            from predictor.contracts.linkage import RecordJournal
            from predictor.time.calendar import load_calendar
            values = {field: parse_json(policy.read_bytes(getattr(args, field))) for field in ("forecast", "label", "evaluation")}
            calendar, _ = load_calendar(root)
            base = root.resolve() / "tmp/linkage-check"
            base.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="interface-", dir=base) as name:
                temporary = Path(name).resolve()
                if not temporary.is_relative_to(base):
                    raise ValueError("Temporary journal escapes workspace")
                journal = RecordJournal(temporary, calendar=calendar)
                forecast = journal.append_forecast(values["forecast"])
                label = journal.append_label(values["label"])
                evaluation = journal.append_evaluation(values["evaluation"])
                snapshot = journal.snapshot(values["forecast"]["forecast_id"])
                print(json.dumps({"status": "ok", "scope": "record_interfaces_only", "forecast_hash": forecast.fingerprint,
                                  "labels": len(snapshot["labels"]), "evaluations": len(snapshot["evaluations"]),
                                  "direction": label.record()["direction"], "binary_up": label.record()["binary_up"],
                                  "correct_binary_up": evaluation.record()["correct_binary_up"], "research_result": "NOT_RUN"}, ensure_ascii=False))
            return 0
        if args.command == "record-check":
            from datetime import date, datetime
            from predictor.contracts.records import parse_record, eligible_at
            from predictor.time.calendar import load_calendar
            record = parse_record(policy.read_bytes(args.path))
            calendar, _ = load_calendar(root)
            cutoff = datetime.fromisoformat(args.cutoff)
            reference = date.fromisoformat(args.event_reference) if len(args.event_reference) == 10 else datetime.fromisoformat(args.event_reference)
            result = eligible_at(record, calendar, cutoff=cutoff, event_reference=reference)
            print(json.dumps({"status": "ok", "scope": "raw_record_contract_only", "availability": result.record(),
                              "research_result": "NOT_RUN"}, ensure_ascii=False))
            return 0 if result.status == "ADMISSIBLE" else 2
        if args.command == "calendar-check":
            from predictor.time.calendar import load_calendar
            calendar, config = load_calendar(root, args.calendar_lock, args.config)
            auction = calendar.auction_window(args.session, **config["auction"])
            print(json.dumps({"status": "ok", "scope": "calendar_contract_only", "clock": calendar.clock(args.session).record(),
                              "horizon_sessions": args.horizon, "target_session": calendar.shift(args.session, args.horizon).isoformat(),
                              "auction_window": auction, "research_result": "NOT_RUN"}, ensure_ascii=False))
            return 0
        if args.command == "protocol-check":
            config = load_protocol(root, args.config, policy=policy)
            if args.release_level:
                require_action(config, args.release_level)
            print(json.dumps({"status": "ok", "scope": "configuration_contract_only",
                              "working_version": config["working_version"], "readiness": readiness(config),
                              "live_trading_enabled": config["protocol"]["live_trading_enabled"],
                              "research_result": "NOT_RUN"}, ensure_ascii=False))
            return 0
        if args.command == "data-read":
            data = policy.read_bytes(args.path)
            print(json.dumps({"status": "ok", "byte_count": len(data),
                              "sha256": hashlib.sha256(data).hexdigest()}, ensure_ascii=False))
            return 0
        config_path = policy.authorize(args.config, operation="config_read") if args.config else None
        config = load_config(config_path)
        policy.check_inputs(config.data_paths)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "error": redact(str(exc))}, ensure_ascii=False), file=sys.stderr)
        return 2
    logger = configure_logging(config.log_level)
    logger.info("工程入口检查完成：%s", config.app_name)
    print(json.dumps({
        "status": "ok",
        "scope": "engineering_shell_only",
        "app_name": config.app_name,
        "version": __version__,
        "timestamp_utc": utc_timestamp(),
        "research_result": "NOT_RUN",
    }, ensure_ascii=False))
    return 0
