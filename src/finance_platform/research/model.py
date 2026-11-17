"""Small deterministic Qlib-equivalent conversion/fit/inference artifact pipeline.

NumPy least squares is actual numerical execution. It is not a Qlib adapter and
does not claim original predictor S03 implementation or economic efficacy.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import numpy as np

from finance_platform.core.contracts import instant, payload
from finance_platform.strategies.contracts import canonical, digest, utc
from .data import ResearchError


@dataclass(frozen=True)
class ModelRow:
    row_id: str
    prediction_at: datetime
    feature_available_at: datetime
    label_mature_at: datetime
    features: tuple[Decimal, ...]
    label: Decimal | None
    scope: str = "SYNTHETIC"
    role: str = "TRAIN"

    def __post_init__(self):
        for key in ("prediction_at", "feature_available_at", "label_mature_at"):
            object.__setattr__(self, key, instant(getattr(self, key)))
        if self.scope not in {"SYNTHETIC", "DEVELOPMENT"} or self.role not in {"TRAIN", "VALIDATION", "TEST"}:
            raise ResearchError("MODEL_PROTECTED_SCOPE_OR_ROLE")
        if self.feature_available_at > self.prediction_at or self.label_mature_at <= self.prediction_at:
            raise ResearchError("MODEL_ROW_CLOCK_INVALID")
        if not self.features or any(type(x) is not Decimal or not x.is_finite() for x in self.features):
            raise ResearchError("MODEL_FEATURE_INVALID")
        if self.label is not None and (type(self.label) is not Decimal or not self.label.is_finite()):
            raise ResearchError("MODEL_LABEL_INVALID")


@dataclass(frozen=True)
class TrainingWindow:
    kind: str
    train_start: datetime
    train_cutoff: datetime
    prediction_start: datetime
    prediction_end: datetime

    def __post_init__(self):
        for key in ("train_start", "train_cutoff", "prediction_start", "prediction_end"):
            object.__setattr__(self, key, instant(getattr(self, key)))
        if self.kind not in {"rolling", "expanding"} or not self.train_start < self.train_cutoff < self.prediction_start <= self.prediction_end:
            raise ResearchError("TRAINING_WINDOW_INVALID")


def _text(value):
    if not np.isfinite(value):
        raise ResearchError("MODEL_NUMERICAL_FAILURE")
    return format(float(value), ".12f")


class LinearPipeline:
    version = "numpy-linear-1.0"

    def fit(self, rows, window: TrainingWindow, *, seed=0, version="1.0.0"):
        if type(seed) is not int or not 0 <= seed < 2**32:
            raise ResearchError("MODEL_SEED_INVALID")
        candidates = [r for r in rows if window.train_start <= r.prediction_at <= window.train_cutoff]
        # Never quietly drop unready labels or permit TEST rows to become rolling train.
        if any(r.role != "TRAIN" or r.label is None or r.label_mature_at > window.train_cutoff for r in candidates):
            raise ResearchError("TRAINING_LABEL_OR_ROLE_NOT_ALLOWED")
        if len(candidates) < 3 or len({r.row_id for r in candidates}) != len(candidates):
            raise ResearchError("TRAINING_SAMPLE_INSUFFICIENT_OR_DUPLICATE")
        dimensions = {len(r.features) for r in candidates}
        if len(dimensions) != 1:
            raise ResearchError("MODEL_DIMENSIONS_INVALID")
        # Conversion, preprocessing fit and numerical fit only inspect candidates.
        x = np.asarray([[float(f) for f in r.features] for r in candidates], dtype=np.float64)
        y = np.asarray([float(r.label) for r in candidates], dtype=np.float64)
        if not np.isfinite(x).all() or not np.isfinite(y).all():
            raise ResearchError("MODEL_NUMERICAL_FAILURE")
        mean, scale = x.mean(axis=0), x.std(axis=0)
        scale[scale == 0] = 1
        z = (x-mean)/scale
        design = np.column_stack((z, np.ones(len(z))))
        coefficients, _, rank, _ = np.linalg.lstsq(design, y, rcond=None)
        artifact = {"pipeline": self.version, "version": version, "numpy_version": np.__version__,
                    "seed": seed, "train_cutoff": payload(window.train_cutoff), "window": payload(window),
                    "training_rows": [r.row_id for r in candidates], "training_hash": digest(payload(candidates)),
                    "mean": list(map(_text, mean)), "scale": list(map(_text, scale)),
                    "coefficients": list(map(_text, coefficients[:-1])), "intercept": _text(coefficients[-1]),
                    "rank": int(rank), "scope": "DEVELOPMENT_ONLY", "label_policy": "mature_train_only"}
        canonical(artifact)
        return artifact

    def infer(self, artifact, rows):
        if artifact.get("pipeline") != self.version or artifact.get("numpy_version") != np.__version__:
            raise ResearchError("MODEL_DEPENDENCY_OR_VERSION_MISMATCH")
        window = artifact["window"]
        for row in rows:
            if row.prediction_at <= utc(artifact["train_cutoff"]) or not utc(window["prediction_start"]) <= row.prediction_at <= utc(window["prediction_end"]):
                raise ResearchError("MODEL_INFERENCE_CLOCK_INVALID")
            if len(row.features) != len(artifact["mean"]):
                raise ResearchError("MODEL_DIMENSIONS_INVALID")
        x = np.asarray([[float(f) for f in r.features] for r in rows], dtype=np.float64)
        if x.size == 0:
            raise ResearchError("MODEL_INFERENCE_EMPTY")
        mean, scale, coefficients = (np.asarray(artifact[k], dtype=np.float64) for k in ("mean", "scale", "coefficients"))
        if not np.isfinite(mean).all() or not np.isfinite(scale).all() or (scale <= 0).any():
            raise ResearchError("MODEL_ARTIFACT_INVALID")
        predictions = ((x-mean)/scale) @ coefficients + float(artifact["intercept"])
        # Keep the first row's payload/score validation before artifact hashing.
        # Reuse only within this call; a later call must hash its current artifact.
        model_hash = None
        result = []
        for r, v in zip(rows, predictions):
            item = {"row_id": r.row_id, "prediction_at": payload(r.prediction_at), "score": _text(v)}
            if model_hash is None:
                model_hash = digest(artifact)
            item["model_hash"] = model_hash
            result.append(item)
        return result


def windows(rows, *, kind, train_size, test_size=1):
    """Explicit chronological rolling/expanding windows; no auto-promoted test rows."""
    ordered = sorted(rows, key=lambda r: r.prediction_at)
    if kind not in {"rolling", "expanding"} or type(train_size) is not int or train_size < 3 or type(test_size) is not int or test_size < 1:
        raise ResearchError("WINDOW_CONFIGURATION_INVALID")
    result = []
    for split in range(train_size, len(ordered), test_size):
        start = 0 if kind == "expanding" else split-train_size
        training = ordered[start:split]
        cutoff = max(r.label_mature_at for r in training)
        test = ordered[split:split+test_size]
        if cutoff >= test[0].prediction_at:
            raise ResearchError("WINDOW_LABEL_NOT_MATURE_BEFORE_TEST")
        result.append(TrainingWindow(kind, training[0].prediction_at, cutoff,
                                     test[0].prediction_at, test[-1].prediction_at))
    return result
