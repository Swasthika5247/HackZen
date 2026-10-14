"""Experimental CatBoost crowd classification integration."""

import json
import math
from pathlib import Path
from typing import Any

from catboost import CatBoostClassifier, Pool
from fastapi import HTTPException

from app.schemas import CrowdPredictionRequest, CrowdPredictionResponse

MODEL_DIR = Path(__file__).resolve().parents[2] / "models"
MODEL_PATH = MODEL_DIR / "comfortroute_crowd_model.cbm"
METADATA_PATH = MODEL_DIR / "comfortroute_model_metadata.json"
EXPECTED_FEATURES = [
    "route", "stop_id", "route_id", "latitude", "longitude",
    "hour_sin", "hour_cos", "day_sin", "day_cos",
]
EXPECTED_CATEGORICAL_FEATURES = ["route", "stop_id", "route_id"]
DATA_NOTICE = (
    "Experimental classifier trained on a 10,000-record sample of historical AC Transit data; "
    "54.73% final test accuracy. It is not validated for live or future crowd forecasting."
)


def _load_model() -> tuple[CatBoostClassifier | None, dict[str, Any] | None, str | None]:
    try:
        with METADATA_PATH.open("r", encoding="utf-8") as metadata_file:
            metadata = json.load(metadata_file)
        if metadata.get("model_type") != "CatBoostClassifier":
            raise ValueError("Metadata model_type must be CatBoostClassifier.")
        if metadata.get("features") != EXPECTED_FEATURES:
            raise ValueError("Metadata feature order does not match the prediction contract.")
        if metadata.get("categorical_features") != EXPECTED_CATEGORICAL_FEATURES:
            raise ValueError("Metadata categorical features do not match the prediction contract.")
        if metadata.get("low_threshold") != 4.0 or metadata.get("high_threshold") != 9.0:
            raise ValueError("Model metadata thresholds are missing or unexpected.")
        if set(metadata.get("classes", [])) != {"High", "Low", "Moderate"}:
            raise ValueError("Metadata must declare High, Low, and Moderate classes.")

        model = CatBoostClassifier()
        model.load_model(str(MODEL_PATH))
        if model.feature_names_ != EXPECTED_FEATURES:
            raise ValueError("CatBoost embedded feature names/order do not match metadata.")
        actual_classes = [str(label) for label in model.classes_]
        if set(actual_classes) != set(metadata["classes"]):
            raise ValueError("CatBoost embedded classes do not match metadata classes.")
        return model, metadata, None
    except Exception as exc:
        return None, None, f"Crowd model initialization failed: {exc}"


# Load once when the service module is initialized.
MODEL, METADATA, MODEL_LOAD_ERROR = _load_model()


def predict_crowd(request: CrowdPredictionRequest) -> CrowdPredictionResponse:
    if MODEL is None:
        raise HTTPException(status_code=503, detail=MODEL_LOAD_ERROR or "Crowd model is unavailable.")

    event_time = request.event_time
    hour = event_time.hour + event_time.minute / 60 + event_time.second / 3600
    day = event_time.weekday()
    row = {
        "route": request.route or "",
        "stop_id": request.stop_id or "",
        "route_id": request.route_id or "",
        "latitude": request.latitude if request.latitude is not None else math.nan,
        "longitude": request.longitude if request.longitude is not None else math.nan,
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "day_sin": math.sin(2 * math.pi * day / 7),
        "day_cos": math.cos(2 * math.pi * day / 7),
    }
    try:
        features = Pool(
            data=[[row[name] for name in EXPECTED_FEATURES]],
            cat_features=EXPECTED_CATEGORICAL_FEATURES,
            feature_names=EXPECTED_FEATURES,
        )
        probabilities = MODEL.predict_proba(features)[0]
        classes = [str(label) for label in MODEL.classes_]
        class_probabilities = {label: float(probability) for label, probability in zip(classes, probabilities)}
        predicted_class = classes[max(range(len(probabilities)), key=lambda index: probabilities[index])]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Crowd prediction failed: {exc}") from exc

    return CrowdPredictionResponse(
        predicted_class=predicted_class,
        class_probabilities=class_probabilities,
        experimental=True,
        data_notice=DATA_NOTICE,
    )
