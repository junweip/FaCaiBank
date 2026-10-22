"""SageMaker scikit-learn container entry point for the transfer-risk model.

The container calls model_fn once, then input_fn -> predict_fn -> output_fn per
request. The model is a logistic regression over transfer features computed by
the banking API before it commits a transfer.
"""

import json
import math
import os


def model_fn(model_dir):
    with open(os.path.join(model_dir, "model.json"), encoding="utf-8") as handle:
        return json.load(handle)


def input_fn(request_body, request_content_type):
    if request_content_type != "application/json":
        raise ValueError(f"Unsupported content type {request_content_type}")
    if isinstance(request_body, (bytes, bytearray)):
        request_body = request_body.decode("utf-8")
    return json.loads(request_body)


def features(payload):
    amount = max(float(payload["amount"]), 0.0)
    hour = int(payload.get("hour_sgt", 12))
    return {
        "log10_amount": math.log10(amount + 1),
        "amount_to_balance": min(max(float(payload.get("amount_to_balance", 0)), 0.0), 2.0),
        "is_night_sgt": 1.0 if 0 <= hour < 5 else 0.0,
        "is_new_payee": 1.0 if payload.get("is_new_payee") else 0.0,
        "velocity": min(float(payload.get("transfers_last_hour", 0)), 20.0) / 20.0,
    }


def predict_fn(input_data, model):
    values = features(input_data)
    z = model["intercept"] + sum(model["weights"][name] * value for name, value in values.items())
    score = 1.0 / (1.0 + math.exp(-z))
    return {"score": round(score, 4), "model_version": model["version"], "features": values}


def output_fn(prediction, accept):
    return json.dumps(prediction)
