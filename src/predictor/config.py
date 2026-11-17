"""Engineering configuration, independent of the research protocol."""

from dataclasses import dataclass
import json
from pathlib import Path
from predictor.secrets import reject_credentials


@dataclass(frozen=True)
class AppConfig:
    app_name: str = "FinanceAnalyze"
    log_level: str = "INFO"
    data_paths: tuple[str, ...] = ()


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        return AppConfig()
    values = json.loads(path.read_text(encoding="utf-8"))
    reject_credentials(values)
    if not isinstance(values, dict):
        raise ValueError("Configuration must be a JSON object")
    unknown = set(values) - {"app_name", "log_level", "data_paths"}
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    app_name = values.get("app_name", "FinanceAnalyze")
    log_level = values.get("log_level", "INFO")
    if not isinstance(app_name, str) or not app_name.strip():
        raise ValueError("app_name must be a non-empty string")
    if not isinstance(log_level, str) or log_level not in {
        "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
    }:
        raise ValueError("log_level must be a standard uppercase logging level")
    data_paths = values.get("data_paths", [])
    if not isinstance(data_paths, list) or any(not isinstance(p, str) or not p.strip() for p in data_paths):
        raise ValueError("data_paths must be a list of non-empty strings")
    return AppConfig(app_name=app_name, log_level=log_level, data_paths=tuple(data_paths))
