"""
UHI intensity regression models (the code that runs inside a SageMaker training job).

Validation follows the leakage-aware plan in the literature survey: the test set is
a set of *spatial blocks* (8 x 8 grid-cell tiles) never seen in training, plus a
grouped 3-fold cross-validation on the training blocks.
Metrics: MAE, RMSE, R-squared (+ CV R-squared). Feature importance: permutation
importance on the held-out blocks.
"""
import time

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupKFold, GroupShuffleSplit, cross_val_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from src import config

ALGORITHMS = {
    "linear_regression": {
        "label": "Ridge Linear Regression (baseline)",
        "image": "683313688378.dkr.ecr.ap-south-1.amazonaws.com/sagemaker-scikit-learn:1.2-1-cpu-py3",
        "defaults": {"alpha": 1.0},
    },
    "random_forest": {
        "label": "Random Forest Regressor",
        "image": "683313688378.dkr.ecr.ap-south-1.amazonaws.com/sagemaker-scikit-learn:1.2-1-cpu-py3",
        "defaults": {"n_estimators": 120, "max_depth": 16, "min_samples_leaf": 3},
    },
    "gradient_boosting": {
        "label": "Histogram Gradient Boosting (XGBoost-style)",
        "image": "991648021394.dkr.ecr.ap-south-1.amazonaws.com/sagemaker-xgboost:1.7-1",
        "defaults": {"max_iter": 400, "learning_rate": 0.06, "max_leaf_nodes": 31},
    },
    "extra_trees": {
        "label": "Extra Trees Regressor",
        "image": "683313688378.dkr.ecr.ap-south-1.amazonaws.com/sagemaker-scikit-learn:1.2-1-cpu-py3",
        "defaults": {"n_estimators": 120, "min_samples_leaf": 3},
    },
}

NUMERIC = [f for f in config.FEATURES if f != "lulc_code"]


def _build_estimator(algorithm, hp):
    if algorithm == "linear_regression":
        pre = ColumnTransformer([("num", StandardScaler(), NUMERIC),
                                 ("cat", OneHotEncoder(handle_unknown="ignore"), ["lulc_code"])])
        return Pipeline([("pre", pre), ("model", Ridge(alpha=float(hp["alpha"])))])
    if algorithm == "random_forest":
        return RandomForestRegressor(n_estimators=int(hp["n_estimators"]), max_depth=int(hp["max_depth"]) or None,
                                     min_samples_leaf=int(hp["min_samples_leaf"]), n_jobs=-1, random_state=42)
    if algorithm == "gradient_boosting":
        return HistGradientBoostingRegressor(max_iter=int(hp["max_iter"]), learning_rate=float(hp["learning_rate"]),
                                             max_leaf_nodes=int(hp["max_leaf_nodes"]), random_state=42)
    if algorithm == "extra_trees":
        return ExtraTreesRegressor(n_estimators=int(hp["n_estimators"]), min_samples_leaf=int(hp["min_samples_leaf"]),
                                   n_jobs=-1, random_state=42)
    raise ValueError(f"Unknown algorithm {algorithm}")


def spatial_blocks(df, block=8):
    rc = df["grid_id"].str.extract(r"-(\d+)-(\d+)$").astype(int)
    return (rc[0] // block).astype(str) + "_" + (rc[1] // block).astype(str)


def train(df: pd.DataFrame, algorithm: str, hyperparameters: dict = None, log=print):
    defaults = dict(ALGORITHMS[algorithm]["defaults"])
    if config.SERVERLESS and "n_estimators" in defaults:   # smaller forests to fit serverless CPU limits
        defaults["n_estimators"] = min(defaults["n_estimators"], 60)
    hp = {**defaults, **(hyperparameters or {})}
    X, y = df[config.FEATURES], df[config.TARGET]
    groups = spatial_blocks(df)

    split = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    tr, te = next(split.split(X, y, groups))
    log(f"Spatial-block split: {len(tr)} train rows / {len(te)} held-out rows "
        f"({groups.iloc[te].nunique()} unseen blocks)")

    est = _build_estimator(algorithm, hp)
    t0 = time.time()
    est.fit(X.iloc[tr], y.iloc[tr])
    fit_s = time.time() - t0
    log(f"Fitted {ALGORITHMS[algorithm]['label']} in {fit_s:.2f}s with {hp}")

    pred = est.predict(X.iloc[te])
    yt = y.iloc[te].to_numpy()
    metrics = {
        "MAE": round(float(mean_absolute_error(yt, pred)), 4),
        "RMSE": round(float(np.sqrt(mean_squared_error(yt, pred))), 4),
        "R2": round(float(r2_score(yt, pred)), 4),
    }
    cv = cross_val_score(_build_estimator(algorithm, hp), X.iloc[tr], y.iloc[tr], groups=groups.iloc[tr],
                         cv=GroupKFold(n_splits=3), scoring="r2", n_jobs=1)
    metrics["CV_R2_mean"] = round(float(cv.mean()), 4)
    metrics["CV_R2_std"] = round(float(cv.std()), 4)
    metrics["train_rows"], metrics["test_rows"] = int(len(tr)), int(len(te))
    metrics["fit_seconds"] = round(fit_s, 2)
    log(f"Held-out metrics: MAE={metrics['MAE']} RMSE={metrics['RMSE']} R2={metrics['R2']} "
        f"| CV R2={metrics['CV_R2_mean']}±{metrics['CV_R2_std']}")

    sample = np.random.default_rng(0).choice(len(te), size=min(1500, len(te)), replace=False)
    pi = permutation_importance(est, X.iloc[te].iloc[sample], yt[sample], n_repeats=3, random_state=0, n_jobs=1)
    importance = sorted(({"feature": f, "importance": round(float(v), 4)}
                         for f, v in zip(config.FEATURES, pi.importances_mean)), key=lambda d: -d["importance"])

    pts = np.random.default_rng(1).choice(len(te), size=min(400, len(te)), replace=False)
    scatter = [{"observed": round(float(yt[i]), 2), "predicted": round(float(pred[i]), 2)} for i in pts]

    est.fit(X, y)   # final model on all data for deployment
    return est, {"hyperparameters": hp, "metrics": metrics, "feature_importance": importance, "scatter": scatter}
