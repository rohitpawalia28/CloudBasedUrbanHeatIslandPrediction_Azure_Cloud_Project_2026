"""
Flask application = the API Gateway stage (/api/v1/*) + the web console pages.

Every /api/v1 route is wrapped by ``api_gateway.endpoint`` which performs Cognito
token / API-key authorization, IAM policy evaluation, throttling, access logging and
CloudWatch metrics - exactly what API Gateway + a Cognito authorizer do in AWS.
"""
import io
import json
import math
import time
from functools import wraps

import pandas as pd
from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for

from src import config
from src.aws_cloud import (api_gateway, aws_adapter, cloudwatch, cognito, glue, iam, lambda_service, rds, s3,
                           sagemaker, sns, step_functions)
from src.backend import cloud_setup
from src.lambdas import handlers

B = config.S3_BUCKET
api = api_gateway.endpoint


def create_app():
    app = Flask(__name__, template_folder="../frontend/templates", static_folder="../frontend/static")
    app.config.update(SECRET_KEY=config.SECRET_KEY, MAX_CONTENT_LENGTH=50 * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    app.json.sort_keys = False
    cloud_setup.provision()

    _dataset_cache = {}

    def processed_df():
        """Model-ready dataset, cached until the processed object's ETag changes."""
        head = s3.head_object(B, "processed/uhi_model_ready.csv")
        if _dataset_cache.get("etag") != head["ETag"]:
            _dataset_cache["df"] = pd.read_csv(s3.local_path(B, "processed/uhi_model_ready.csv"))
            _dataset_cache["etag"] = head["ETag"]
        return _dataset_cache["df"]

    # ------------------------------------------------------------ helpers -----
    def current_user():
        token = session.get("id_token")
        if not token:
            return None
        try:
            c = cognito.verify_token(token)
            return {"email": c["email"], "name": c.get("name"), "group": c["cognito:groups"][0]}
        except cognito.AuthError:
            session.clear()
            return None

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            user = current_user()
            if not user:
                return redirect(url_for("login", next=request.path))
            g.user = user
            return fn(*a, **kw)
        return wrapper

    @app.context_processor
    def inject():
        user = current_user()
        return {"cfg": config, "user": user, "unread": sns.unread_count() if user else 0,
                "region": config.AWS_REGION, "invoke_url": api_gateway.INVOKE_URL,
                "can": (lambda action: bool(user) and iam.group_allowed(user["group"], action))}

    def clean(obj):
        """Make floats JSON-safe (NaN/inf -> None)."""
        if isinstance(obj, float):
            return None if math.isnan(obj) or math.isinf(obj) else obj
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [clean(v) for v in obj]
        return obj

    def ok(data, status=200):
        return jsonify(clean(data)), status

    # ---------------------------------------------------------- auth pages ----
    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            try:
                res = cognito.initiate_auth(request.form.get("email"), request.form.get("password"))
                session["id_token"] = res["IdToken"]
                rds.audit(res["claims"]["email"], "SIGN_IN", "web console")
                nxt = request.args.get("next") or "/"
                return redirect(nxt if nxt.startswith("/") else "/")
            except cognito.AuthError as exc:
                error = str(exc)
        return render_template("login.html", error=error, mode="login")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        error = None
        if request.method == "POST":
            try:
                group = request.form.get("group", "viewer")
                if group == "admin":
                    group = "analyst"
                cognito.sign_up(request.form.get("email"), request.form.get("password"), request.form.get("name"), group)
                res = cognito.initiate_auth(request.form.get("email"), request.form.get("password"))
                session["id_token"] = res["IdToken"]
                return redirect("/")
            except cognito.AuthError as exc:
                error = str(exc)
        return render_template("login.html", error=error, mode="register", policy=cognito.PASSWORD_POLICY)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------- console pages ----
    pages = {
        "/": ("dashboard.html", "Dashboard"), "/map": ("map.html", "Heat Risk Map"),
        "/predict": ("predict.html", "Predict UHI"), "/data-lake": ("datalake.html", "Data Lake (S3)"),
        "/pipeline": ("pipeline.html", "Pipeline"), "/models": ("models.html", "SageMaker Models"),
        "/monitoring": ("monitoring.html", "CloudWatch"), "/alerts": ("alerts.html", "SNS Alerts"),
        "/database": ("database.html", "RDS Database"), "/api-access": ("api_access.html", "API Gateway"),
        "/admin": ("admin.html", "Cognito & IAM"), "/architecture": ("architecture.html", "Architecture"),
        "/aws": ("aws_account.html", "Real AWS"),
    }
    for path, (tpl, title) in pages.items():
        def view(tpl=tpl, title=title):
            return render_template(tpl, title=title, active=request.path)
        app.add_url_rule(path, endpoint="page_" + tpl.split(".")[0], view_func=login_required(view))

    # =================================================== API GATEWAY /api/v1 ==
    @app.route("/api/v1/health")
    @api("uhi:Health", auth=False, description="Liveness probe")
    def health():
        return ok({"status": "healthy", "region": config.AWS_REGION, "mode": config.CLOUD_MODE,
                   "endpoint": (sagemaker.describe_endpoint() or {}).get("EndpointStatus", "NotDeployed"),
                   "time": cloudwatch.now_iso()})

    @app.route("/api/v1/me")
    @api("uhi:Read", description="Current identity")
    def me():
        return ok(g.identity)

    # ---------------------------------------------------- platform status ----
    @app.route("/api/v1/platform/status")
    @api("uhi:Read", description="Service health for every AWS service")
    def platform_status():
        ep = sagemaker.describe_endpoint()
        execs = step_functions.list_executions(1)
        objs = s3.list_objects(B)
        return ok({
            "mode": config.CLOUD_MODE, "aws": aws_adapter.status(),
            "pipelineRunning": step_functions.running(),
            "lastExecution": execs[0] if execs else None,
            "services": [
                {"service": "Amazon S3", "status": "Available", "detail": f"{len(objs)} objects, {sum(o['Size'] for o in objs) / 1e6:.1f} MB"},
                {"service": "AWS Lambda", "status": "Available", "detail": f"{len(lambda_service.list_functions())} functions"},
                {"service": "AWS Glue", "status": "Available", "detail": f"{len(glue.job_runs())} job runs, {len(glue.catalog_tables())} catalog tables"},
                {"service": "Amazon SageMaker", "status": ep["EndpointStatus"] if ep else "NoEndpoint",
                 "detail": f"{len(sagemaker.list_training_jobs())} training jobs, endpoint {ep['AlgorithmLabel'] if ep else '-'}"},
                {"service": "Amazon RDS", "status": "Available", "detail": f"{len(rds.tables())} tables ({rds.INSTANCE['DBInstanceClass']})"},
                {"service": "Amazon API Gateway", "status": "Deployed", "detail": f"{len(api_gateway.ROUTES)} routes on stage {api_gateway.STAGE}"},
                {"service": "Amazon Cognito", "status": "Active", "detail": f"{len(cognito.list_users())} users in pool"},
                {"service": "Amazon CloudWatch", "status": "Available",
                 "detail": f"{sum(1 for a in cloudwatch.list_alarms() if a['state'] == 'ALARM')} alarm(s) in ALARM"},
                {"service": "Amazon SNS", "status": "Available", "detail": f"{len(sns.list_topics())} topics, {sns.unread_count()} unread"},
                {"service": "AWS Step Functions", "status": "Running" if step_functions.running() else "Idle",
                 "detail": f"{len(step_functions.list_executions(100))} executions"},
            ],
        })

    # ------------------------------------------------------ UHI analytics -----
    @app.route("/api/v1/overview")
    @api("uhi:Read", description="Dashboard KPIs")
    def overview():
        latest = rds.query_one("SELECT MAX(obs_date) d FROM grid_predictions")["d"]
        if not latest:
            return ok({"ready": False, "pipelineRunning": step_functions.running()})
        k = rds.query_one("""SELECT COUNT(*) cells, AVG(uhi_predicted) mean_uhi, MAX(uhi_predicted) max_uhi,
                             AVG(lst_celsius) mean_lst, SUM(risk_level IN ('High','Severe')) hot
                             FROM grid_predictions WHERE obs_date=?""", (latest,))
        risk = rds.query("SELECT risk_level, COUNT(*) n FROM grid_predictions WHERE obs_date=? GROUP BY risk_level", (latest,))
        trend = rds.query("""SELECT obs_date, AVG(uhi_predicted) mean_uhi, MAX(uhi_predicted) max_uhi, AVG(lst_celsius) mean_lst
                             FROM grid_predictions GROUP BY obs_date ORDER BY obs_date""")
        by_class = rds.query("SELECT zone, mean_uhi, max_uhi, mean_ndvi, cells FROM hotspot_summaries WHERE obs_date=? ORDER BY mean_uhi DESC", (latest,))
        ep = sagemaker.describe_endpoint()
        return ok({"ready": True, "latestDate": latest, "kpi": k, "risk": risk, "trend": trend, "byClass": by_class,
                   "model": {"label": ep["AlgorithmLabel"], "version": ep["ModelPackageVersion"], "metrics": ep["Metrics"]} if ep else None,
                   "studyArea": config.STUDY_AREA})

    @app.route("/api/v1/dates")
    @api("uhi:Read", description="Available observation dates")
    def dates():
        return ok([r["obs_date"] for r in rds.query("SELECT DISTINCT obs_date FROM grid_predictions ORDER BY obs_date")])

    @app.route("/api/v1/map")
    @api("uhi:Read", description="Grid cells for a date (layer data)")
    def map_cells():
        date = request.args.get("date") or rds.query_one("SELECT MAX(obs_date) d FROM grid_predictions")["d"]
        rows = rds.query("""SELECT grid_id, latitude lat, longitude lon, uhi_predicted uhi, uhi_observed obs, lst_celsius lst,
                            ndvi, ndbi, lulc_code, risk_level risk FROM grid_predictions WHERE obs_date=?""", (date,))
        a = config.STUDY_AREA
        return ok({"date": date, "cells": rows, "cellSize": {"lat": (a["lat_max"] - a["lat_min"]) / (a["grid_size"] - 1),
                                                           "lon": (a["lon_max"] - a["lon_min"]) / (a["grid_size"] - 1)},
                   "lulc": config.LULC_CLASSES})

    @app.route("/api/v1/hotspots")
    @api("uhi:Read", description="Hottest cells")
    def hotspots():
        date = request.args.get("date") or rds.query_one("SELECT MAX(obs_date) d FROM grid_predictions")["d"]
        limit = min(int(request.args.get("limit", 10)), 100)
        return ok(rds.query("""SELECT grid_id, latitude, longitude, uhi_predicted, lst_celsius, ndvi, lulc_code, risk_level
                               FROM grid_predictions WHERE obs_date=? ORDER BY uhi_predicted DESC LIMIT ?""", (date, limit)))

    @app.route("/api/v1/cell/<grid_id>")
    @api("uhi:Read", description="Full feature record + history for one grid cell")
    def cell(grid_id):
        df = processed_df()
        rows = df[df["grid_id"] == grid_id]
        if rows.empty:
            raise KeyError(f"grid cell {grid_id}")
        hist = rds.query("SELECT obs_date, uhi_predicted, uhi_observed, lst_celsius, ndvi FROM grid_predictions WHERE grid_id=? ORDER BY obs_date", (grid_id,))
        latest = rows.sort_values("timestamp").iloc[-1].to_dict()
        return ok({"grid_id": grid_id, "latest": latest, "history": hist})

    # --------------------------------------------------- prediction (SM) ------
    def _record_prediction(source, lat, lon, inputs, value, latency):
        rds.execute("""INSERT INTO prediction_requests (ts, user_email, source, latitude, longitude, inputs, uhi_predicted,
                       risk_level, model_version, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (cloudwatch.now_iso(), g.identity["email"], source, lat, lon, json.dumps(inputs), value,
                     config.risk_level(value), sagemaker.endpoint_model_version(), round(latency, 2)))
        aws_adapter.mirror_prediction({"timestamp": cloudwatch.now_iso(), "user": g.identity["email"], "source": source,
                                       "latitude": lat, "longitude": lon, "uhi_predicted": float(value),
                                       "risk_level": config.risk_level(value),
                                       "model_version": str(sagemaker.endpoint_model_version()),
                                       "latency_ms": round(latency, 2), "features": inputs})

    @app.route("/api/v1/predict", methods=["POST"])
    @api("uhi:Predict", description="Real-time UHI prediction (SageMaker InvokeEndpoint)")
    def predict():
        body = request.get_json(force=True, silent=True) or {}
        start = time.time()
        base, near = {}, None
        if "latitude" in body and "longitude" in body:
            df = processed_df()
            last = df[df["timestamp"] == df["timestamp"].max()]
            d2 = (last["latitude"] - float(body["latitude"])) ** 2 + (last["longitude"] - float(body["longitude"])) ** 2
            near = last.loc[d2.idxmin()]
            base = {f: float(near[f]) for f in config.FEATURES}
        features = {**base, **{f: float(body["features"][f]) for f in body.get("features", {}) if f in config.FEATURES}}
        missing = [f for f in config.FEATURES if f not in features]
        if missing:
            raise ValueError(f"missing features {missing} (send all features, or latitude/longitude)")
        value = sagemaker.invoke_endpoint([features])[0]
        baseline = sagemaker.invoke_endpoint([base])[0] if base and base != features else None
        latency = (time.time() - start) * 1000
        _record_prediction("map" if near is not None else "form", body.get("latitude"), body.get("longitude"),
                           features, value, latency)
        return ok({"uhi_intensity_celsius": value, "risk_level": config.risk_level(value),
                   "baseline_uhi": baseline, "features": features,
                   "nearest_cell": None if near is None else {"grid_id": near["grid_id"], "lulc_class": near["lulc_class"],
                                                               "lst_celsius": near["lst_celsius"], "date": near["timestamp"]},
                   "model_version": sagemaker.endpoint_model_version(), "latency_ms": round(latency, 2),
                   "endpoint": sagemaker.ENDPOINT_NAME,
                   "disclaimer": "Satellite-derived surface UHI estimate for decision support only."})

    @app.route("/api/v1/predict/batch", methods=["POST"])
    @api("uhi:Predict", description="Batch prediction from an uploaded CSV")
    def predict_batch():
        f = request.files.get("file")
        if not f:
            raise ValueError("upload a CSV in the 'file' field")
        df = pd.read_csv(f)
        preds = sagemaker.invoke_endpoint(df.to_dict("records"))
        df["uhi_predicted"] = preds
        df["risk_level"] = [config.risk_level(p) for p in preds]
        key = f"predictions/batch/{g.identity['email'].split('@')[0]}-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        s3.put_object(B, key, buf.getvalue(), "text/csv", fire_events=False)
        rds.audit(g.identity["email"], "BATCH_PREDICT", f"{len(df)} rows -> {key}")
        return ok({"rows": len(df), "s3_key": key, "download": s3.presigned_url(B, key),
                   "preview": df.head(25).to_dict("records")})

    @app.route("/api/v1/predict/template")
    @api("uhi:Read", description="Sample CSV for batch prediction")
    def predict_template():
        df = processed_df()
        sample = df.sample(20, random_state=7)[["grid_id"] + config.FEATURES]
        return Response(sample.to_csv(index=False), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=uhi_batch_template.csv"})

    @app.route("/api/v1/predictions/history")
    @api("uhi:Read", description="Recent prediction requests")
    def prediction_history():
        return ok(rds.query("SELECT * FROM prediction_requests ORDER BY id DESC LIMIT 50"))

    @app.route("/api/v1/feature-ranges")
    @api("uhi:Read", description="Min/max/mean of every model feature")
    def feature_ranges():
        df = processed_df()
        return ok({f: {"min": round(float(df[f].min()), 3), "max": round(float(df[f].max()), 3),
                       "mean": round(float(df[f].mean()), 3)} for f in config.FEATURES} | {"_lulc": config.LULC_CLASSES})

    # ------------------------------------------------------------------ S3 ----
    @app.route("/api/v1/s3/buckets")
    @api("s3:ListBucket", description="List buckets")
    def s3_buckets():
        return ok({"buckets": s3.list_buckets(), "notifications": s3.notification_config(B)})

    @app.route("/api/v1/s3/objects")
    @api("s3:ListBucket", description="List objects under a prefix")
    def s3_objects():
        return ok(s3.list_objects(request.args.get("bucket", B), request.args.get("prefix", "")))

    @app.route("/api/v1/s3/upload", methods=["POST"])
    @api("s3:PutObject", description="Upload an object (fires S3 event notifications)")
    def s3_upload():
        f = request.files.get("file")
        prefix = request.form.get("prefix", "raw/")
        if not f or not f.filename:
            raise ValueError("no file")
        if not prefix.endswith("/"):
            prefix += "/"
        name = f.filename.replace("\\", "/").split("/")[-1]
        meta = s3.put_object(B, prefix + name, f.read(), f.mimetype, metadata={"uploaded-by": g.identity["email"]})
        rds.audit(g.identity["email"], "S3_UPLOAD", prefix + name)
        return ok({"uploaded": meta, "triggered": [n["Id"] for n in s3.notification_config(B)
                                                  if (prefix + name).startswith(n["Filter"]["Prefix"])
                                                  and name.endswith(n["Filter"]["Suffix"])]}, 201)

    @app.route("/api/v1/s3/presign")
    @api("s3:GetObject", description="Create a pre-signed download URL")
    def s3_presign():
        return ok({"url": s3.presigned_url(B, request.args["key"], int(request.args.get("expires", 900)))})

    @app.route("/api/v1/s3/download")
    def s3_download():
        key, bucket = request.args.get("key", ""), request.args.get("bucket", B)
        if request.args.get("signature"):
            if not s3.verify_presigned(bucket, key, request.args.get("expires", 0), request.args["signature"]):
                return jsonify({"message": "SignatureDoesNotMatch or URL expired"}), 403
        elif not current_user():
            return jsonify({"message": "Unauthorized"}), 401
        try:
            data, head = s3.get_object(bucket, key), s3.head_object(bucket, key)
        except s3.NoSuchKey:
            return jsonify({"message": "NoSuchKey"}), 404
        return Response(data, mimetype=head["ContentType"],
                        headers={"Content-Disposition": f"attachment; filename={key.split('/')[-1]}", "ETag": head["ETag"]})

    @app.route("/api/v1/s3/object", methods=["GET", "DELETE"])
    @api("s3:GetObject", description="Object metadata / versions, or delete")
    def s3_object():
        key = request.args["key"]
        if request.method == "DELETE":
            if not iam.group_allowed(g.identity["group"], "s3:DeleteObject", f"arn:aws:s3:::{B}/{key}") or key.startswith("raw/"):
                return ok({"message": "AccessDenied: raw zone is immutable / role cannot delete"}, 403)
            s3.delete_object(B, key)
            rds.audit(g.identity["email"], "S3_DELETE", key)
            return ok({"deleted": key})
        head = s3.head_object(B, key)
        preview = None
        if head["ContentType"] in ("text/csv", "application/json", "application/geo+json") and head["Size"] < 30_000_000:
            text = s3.get_object(B, key).decode("utf-8", "ignore")
            preview = "\n".join(text.splitlines()[:15])[:6000]
        return ok({"head": head, "versions": s3.list_versions(B, key), "preview": preview,
                   "uri": f"s3://{B}/{key}", "arn": f"arn:aws:s3:::{B}/{key}"})

    # -------------------------------------------------------------- Lambda ----
    @app.route("/api/v1/lambda/functions")
    @api("lambda:ListFunctions", description="Lambda functions + recent invocations")
    def lambda_functions():
        return ok({"functions": lambda_service.list_functions(), "invocations": lambda_service.invocations(30),
                   "settings": handlers.SETTINGS})

    @app.route("/api/v1/lambda/invoke/<name>", methods=["POST"])
    @api("lambda:InvokeFunction", description="Invoke a Lambda function")
    def lambda_invoke(name):
        payload = request.get_json(silent=True) or {}
        return ok(lambda_service.invoke(name, payload))

    # ---------------------------------------------------------------- Glue ----
    @app.route("/api/v1/glue")
    @api("glue:GetJobRuns", description="Glue jobs, runs and Data Catalog")
    def glue_info():
        return ok({"jobs": glue.list_jobs(), "runs": glue.job_runs()[:20], "catalog": glue.catalog_tables(),
                   "database": glue.DATABASE})

    @app.route("/api/v1/glue/run", methods=["POST"])
    @api("glue:StartJobRun", description="Start the ETL job")
    def glue_run():
        rid = glue.start_job_run("uhi-etl-preprocess", {"--trigger": "console", "--user": g.identity["email"]})
        return ok({"JobRunId": rid}, 202)

    @app.route("/api/v1/glue/crawl", methods=["POST"])
    @api("glue:StartCrawler", description="Run the processed-zone crawler")
    def glue_crawl():
        return ok({"tables": glue.run_crawler()})

    # ----------------------------------------------------------- SageMaker ----
    @app.route("/api/v1/sagemaker")
    @api("sagemaker:Describe", description="Training jobs, model registry, endpoint")
    def sm_info():
        from src.ai_model.trainer import ALGORITHMS
        jobs = [{k: v for k, v in j.items() if k != "Scatter"} for j in sagemaker.list_training_jobs()]
        return ok({"jobs": jobs, "packages": sagemaker.list_model_packages(), "endpoint": sagemaker.describe_endpoint(),
                   "algorithms": {k: {"label": v["label"], "defaults": v["defaults"]} for k, v in ALGORITHMS.items()}})

    @app.route("/api/v1/sagemaker/jobs/<name>")
    @api("sagemaker:Describe", description="Describe one training job")
    def sm_job(name):
        job = sagemaker.describe_training_job(name)
        if not job:
            raise KeyError(name)
        return ok(job)

    @app.route("/api/v1/sagemaker/train", methods=["POST"])
    @api("sagemaker:CreateTrainingJob", description="Start a training job")
    def sm_train():
        body = request.get_json(force=True)
        hp = {k: v for k, v in (body.get("hyperparameters") or {}).items() if v not in ("", None)}
        name = sagemaker.create_training_job(body["algorithm"], hp, body.get("instanceType", "ml.m5.xlarge"),
                                             created_by=g.identity["email"])
        rds.audit(g.identity["email"], "CREATE_TRAINING_JOB", name)
        return ok({"TrainingJobName": name}, 202)

    @app.route("/api/v1/sagemaker/packages/<int:version>/approval", methods=["POST"])
    @api("sagemaker:UpdateModelPackage", description="Approve / reject a model version")
    def sm_approve(version):
        status = (request.get_json(force=True) or {}).get("status")
        return ok(sagemaker.update_approval(version, status))

    @app.route("/api/v1/sagemaker/deploy", methods=["POST"])
    @api("sagemaker:CreateEndpoint", description="Deploy a model version to the endpoint")
    def sm_deploy():
        version = int((request.get_json(force=True) or {})["version"])
        ep = sagemaker.deploy(version)
        res = sagemaker.batch_transform(log=lambda m: cloudwatch.log("/aws/sagemaker/TransformJobs", m))
        rds.audit(g.identity["email"], "DEPLOY_MODEL", f"v{version}")
        return ok({"endpoint": ep, "batchTransform": res})

    # ------------------------------------------------------ Step Functions ----
    @app.route("/api/v1/pipeline")
    @api("states:DescribeExecution", description="State machine + executions")
    def pipeline_info():
        return ok({"definition": step_functions.definition(cloud_setup.PIPELINE),
                   "machines": step_functions.list_state_machines(), "executions": step_functions.list_executions(20)})

    @app.route("/api/v1/pipeline/executions/<name>")
    @api("states:DescribeExecution", description="Describe execution")
    def pipeline_exec(name):
        ex = step_functions.describe_execution(name)
        if not ex:
            raise KeyError(name)
        return ok(ex)

    @app.route("/api/v1/pipeline/run", methods=["POST"])
    @api("states:StartExecution", description="Start the end-to-end pipeline")
    def pipeline_run():
        body = request.get_json(silent=True) or {}
        name = cloud_setup.run_pipeline(g.identity["email"], bool(body.get("regenerate")))
        rds.audit(g.identity["email"], "START_PIPELINE", name)
        return ok({"execution": name}, 202)

    # ---------------------------------------------------------- CloudWatch ----
    @app.route("/api/v1/cloudwatch/logs")
    @api("logs:FilterLogEvents", description="Log groups + filtered events")
    def cw_logs():
        return ok({"groups": cloudwatch.log_groups(),
                   "events": cloudwatch.filter_logs(request.args.get("group") or None, request.args.get("level") or None,
                                                    request.args.get("q") or None, int(request.args.get("limit", 200)))})

    @app.route("/api/v1/cloudwatch/metrics")
    @api("cloudwatch:GetMetricData", description="Metric list + key service statistics")
    def cw_metrics():
        minutes = int(request.args.get("minutes", 60))
        period = max(60, minutes * 60 // 30)
        series = {
            "api_requests": cloudwatch.metric_statistics("AWS/ApiGateway", "Count", period, minutes),
            "api_latency": cloudwatch.metric_statistics("AWS/ApiGateway", "Latency", period, minutes, "Average"),
            "api_4xx": cloudwatch.metric_statistics("AWS/ApiGateway", "4XXError", period, minutes),
            "lambda_invocations": cloudwatch.metric_statistics("AWS/Lambda", "Invocations", period, minutes),
            "lambda_duration": cloudwatch.metric_statistics("AWS/Lambda", "Duration", period, minutes, "Average"),
            "sm_invocations": cloudwatch.metric_statistics("AWS/SageMaker", "Invocations", period, minutes),
            "sm_latency": cloudwatch.metric_statistics("AWS/SageMaker", "ModelLatency", period, minutes, "Average"),
            "s3_puts": cloudwatch.metric_statistics("AWS/S3", "PutRequests", period, minutes),
            "rds_reads": cloudwatch.metric_statistics("AWS/RDS", "ReadIOPS", period, minutes),
        }
        totals = {
            "apiRequests": cloudwatch.metric_total("AWS/ApiGateway", "Count", minutes),
            "api4xx": cloudwatch.metric_total("AWS/ApiGateway", "4XXError", minutes),
            "api5xx": cloudwatch.metric_total("AWS/ApiGateway", "5XXError", minutes),
            "lambdaInvocations": cloudwatch.metric_total("AWS/Lambda", "Invocations", minutes),
            "lambdaErrors": cloudwatch.metric_total("AWS/Lambda", "Errors", minutes),
            "predictions": cloudwatch.metric_total("AWS/SageMaker", "Invocations", minutes),
        }
        return ok({"series": series, "totals": totals, "metrics": cloudwatch.list_metrics(), "periodSeconds": period})

    @app.route("/api/v1/cloudwatch/alarms", methods=["GET", "POST", "DELETE"])
    @api("cloudwatch:GetMetricData", description="List / create / delete alarms")
    def cw_alarms():
        if request.method in ("POST", "DELETE") and not iam.group_allowed(g.identity["group"], "cloudwatch:PutMetricAlarm"):
            return ok({"message": "AccessDenied: cloudwatch:PutMetricAlarm"}, 403)
        if request.method == "POST":
            b = request.get_json(force=True)
            cloudwatch.put_alarm(b["name"], b["namespace"], b["metric"], b["comparison"], float(b["threshold"]),
                                 int(b.get("period", 300)), b.get("statistic", "Maximum"), b.get("snsTopic") or None)
        if request.method == "DELETE":
            cloudwatch.delete_alarm(request.args["name"])
        return ok(cloudwatch.list_alarms())

    # ----------------------------------------------------------------- SNS ----
    @app.route("/api/v1/sns")
    @api("sns:ListTopics", description="Topics, subscriptions, messages, settings")
    def sns_info():
        return ok({"topics": sns.list_topics(), "subscriptions": sns.list_subscriptions(),
                   "messages": sns.messages(limit=60), "threshold": handlers.SETTINGS["alert_threshold"],
                   "autoEtl": handlers.SETTINGS["auto_etl_on_upload"]})

    @app.route("/api/v1/sns/subscribe", methods=["POST"])
    @api("sns:Subscribe", description="Subscribe an endpoint to a topic")
    def sns_subscribe():
        b = request.get_json(force=True)
        if b.get("protocol") not in ("email", "sms", "https", "app"):
            raise ValueError("protocol must be email, sms, https or app")
        if not b.get("endpoint"):
            raise ValueError("endpoint is required")
        return ok({"SubscriptionId": sns.subscribe(b["topic"], b["protocol"], b["endpoint"])}, 201)

    @app.route("/api/v1/sns/subscriptions/<sid>", methods=["DELETE"])
    @api("sns:Unsubscribe", description="Remove a subscription")
    def sns_unsub(sid):
        sns.unsubscribe(sid)
        return ok({"deleted": sid})

    @app.route("/api/v1/sns/publish", methods=["POST"])
    @api("sns:Publish", description="Publish a test message")
    def sns_publish():
        b = request.get_json(force=True)
        return ok({"MessageId": sns.publish(b.get("topic", "uhi-heat-alerts"), b.get("subject", "Test"),
                                            b.get("message", f"Test notification from {g.identity['email']}"))}, 201)

    @app.route("/api/v1/sns/read", methods=["POST"])
    @api("uhi:Read", description="Mark notifications read")
    def sns_read():
        sns.mark_all_read()
        return ok({"unread": 0})

    @app.route("/api/v1/settings", methods=["POST"])
    @api("lambda:UpdateFunctionConfiguration", description="Alert threshold / auto-ETL settings")
    def settings():
        b = request.get_json(force=True)
        if "threshold" in b:
            handlers.SETTINGS["alert_threshold"] = float(b["threshold"])
        if "autoEtl" in b:
            handlers.SETTINGS["auto_etl_on_upload"] = bool(b["autoEtl"])
        return ok(handlers.SETTINGS)

    # ----------------------------------------------------------------- RDS ----
    @app.route("/api/v1/rds")
    @api("rds:Query", description="Instance details, tables, snapshots")
    def rds_info():
        return ok({"instance": rds.INSTANCE, "tables": rds.tables(), "snapshots": rds.snapshots()[:10],
                   "audit": rds.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT 30")})

    @app.route("/api/v1/rds/query", methods=["POST"])
    @api("rds:Query", description="Read-only SQL query editor")
    def rds_query():
        sql = (request.get_json(force=True) or {}).get("sql", "")
        t0 = time.time()
        res = rds.read_only_query(sql)
        rds.audit(g.identity["email"], "SQL_QUERY", sql[:200])
        return ok(res | {"elapsedMs": round((time.time() - t0) * 1000, 2)})

    @app.route("/api/v1/rds/snapshot", methods=["POST"])
    @api("rds:CreateDBSnapshot", description="Create manual snapshot")
    def rds_snapshot():
        return ok({"snapshot": rds.snapshot()}, 201)

    # ------------------------------------------------------ API Gateway -------
    @app.route("/api/v1/apigateway")
    @api("apigateway:ManageOwnKeys", description="Routes, stage and API keys")
    def apigw_info():
        owner = None if g.identity["group"] == "admin" else g.identity["email"]
        routes = []
        for rule in app.url_map.iter_rules():
            if rule.rule.startswith("/api/v1"):
                meta = next((r for r in api_gateway.ROUTES if r["function"] == rule.endpoint), {})
                routes.append({"path": rule.rule, "methods": sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS")),
                               "action": meta.get("action", "presigned"), "auth": meta.get("auth", False),
                               "description": meta.get("description", "Pre-signed S3 download")})
        return ok({"apiId": api_gateway.API_ID, "stage": api_gateway.STAGE, "invokeUrl": api_gateway.INVOKE_URL,
                   "localUrl": request.host_url.rstrip("/") + "/api/v1", "rateLimitPerMin": config.API_RATE_LIMIT_PER_MIN,
                   "routes": sorted(routes, key=lambda r: r["path"]), "keys": api_gateway.list_api_keys(owner)})

    @app.route("/api/v1/apigateway/keys", methods=["POST"])
    @api("apigateway:ManageOwnKeys", description="Create an API key")
    def apigw_create_key():
        name = (request.get_json(force=True) or {}).get("name") or "my-key"
        return ok(api_gateway.create_api_key(name[:40], g.identity["email"], g.identity["group"]), 201)

    @app.route("/api/v1/apigateway/keys/<kid>", methods=["DELETE"])
    @api("apigateway:ManageOwnKeys", description="Delete an API key")
    def apigw_delete_key(kid):
        api_gateway.delete_api_key(kid, None if g.identity["group"] == "admin" else g.identity["email"])
        return ok({"deleted": kid})

    # ---------------------------------------------------- Cognito / IAM ------
    @app.route("/api/v1/cognito/users")
    @api("cognito:AdminUserList", description="List user-pool users")
    def cognito_users():
        return ok({"userPoolId": cognito.USER_POOL_ID, "clientId": cognito.CLIENT_ID, "users": cognito.list_users(),
                   "groups": cognito.GROUPS, "passwordPolicy": cognito.PASSWORD_POLICY})

    @app.route("/api/v1/cognito/users/<path:email>", methods=["POST"])
    @api("cognito:AdminUserUpdate", description="Change a user's group / status")
    def cognito_update(email):
        b = request.get_json(force=True)
        if email == g.identity["email"]:
            raise ValueError("You cannot change your own group or status")
        cognito.admin_update_user(email, b.get("group"), b.get("status"))
        rds.audit(g.identity["email"], "ADMIN_UPDATE_USER", f"{email} {b}")
        return ok(cognito.list_users())

    @app.route("/api/v1/aws/status")
    @api("uhi:Read", description="Real AWS bridge status (CLOUD_MODE=aws)")
    def aws_status():
        return ok(aws_adapter.status())

    @app.route("/api/v1/aws/lambda/heat-check", methods=["POST"])
    @api("lambda:InvokeFunction", description="Invoke the real uhi-heat-alert-checker Lambda")
    def aws_heat_check():
        result = aws_adapter.invoke_heat_checker()
        rds.audit(g.identity["email"], "INVOKE_REAL_LAMBDA", json.dumps(result)[:300])
        return ok(result)

    @app.route("/api/v1/iam/roles")
    @api("iam:ListRoles", description="IAM roles and policies")
    def iam_roles():
        return ok(iam.list_roles())

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"message": "Not Found"}), 404
        if not current_user():
            return redirect(url_for("login"))
        return render_template("404.html", title="Not found", active=""), 404

    return app
