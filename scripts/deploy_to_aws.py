"""
OPTIONAL - mirror the project into a REAL AWS account (Free-Tier friendly).

Creates / updates:
  * S3 bucket  <S3_BUCKET>  (versioning + AES-256 encryption + public access block)
    and uploads every object of the emulated data lake (raw, processed,
    model-artifacts, predictions)
  * SNS topic  uhi-heat-alerts  (+ optional e-mail subscription)
  * CloudWatch alarm on the custom metric UHI/MaxIntensity -> SNS topic

Requirements:  pip install -r requirements-aws.txt   and   aws configure
Usage:
    python scripts/deploy_to_aws.py --dry-run          # show what would happen
    python scripts/deploy_to_aws.py --email you@x.com  # deploy for real
Remove everything again with:  python scripts/deploy_to_aws.py --destroy
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import config  # noqa: E402

TOPIC = "uhi-heat-alerts"
LAKE = config.CLOUD_ROOT / "s3" / config.S3_BUCKET


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--destroy", action="store_true")
    ap.add_argument("--email", help="subscribe this e-mail to heat alerts")
    a = ap.parse_args()

    files = [p for p in LAKE.rglob("*") if p.is_file()] if LAKE.exists() else []
    if not files and not a.destroy:
        sys.exit("Emulated data lake is empty - run  python run.py --pipeline-only  first.")
    print(f"Region {config.AWS_REGION} | bucket {config.S3_BUCKET} | {len(files)} objects")
    if a.dry_run:
        for p in files[:30]:
            print("  would upload", p.relative_to(LAKE).as_posix())
        return

    import boto3
    s3 = boto3.client("s3", region_name=config.AWS_REGION)
    sns = boto3.client("sns", region_name=config.AWS_REGION)
    cw = boto3.client("cloudwatch", region_name=config.AWS_REGION)
    bucket = config.S3_BUCKET

    if a.destroy:
        res = boto3.resource("s3", region_name=config.AWS_REGION).Bucket(bucket)
        res.object_versions.delete()
        res.delete()
        topic = sns.create_topic(Name=TOPIC)["TopicArn"]
        sns.delete_topic(TopicArn=topic)
        cw.delete_alarms(AlarmNames=["uhi-high-intensity"])
        print("All UHI resources deleted.")
        return

    try:
        kw = {} if config.AWS_REGION == "us-east-1" else {
            "CreateBucketConfiguration": {"LocationConstraint": config.AWS_REGION}}
        s3.create_bucket(Bucket=bucket, **kw)
        print("created bucket", bucket)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        print("bucket exists", bucket)
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    for p in files:
        key = p.relative_to(LAKE).as_posix()
        s3.upload_file(str(p), bucket, key)
        print("  uploaded", key)

    topic = sns.create_topic(Name=TOPIC)["TopicArn"]
    print("SNS topic", topic)
    if a.email:
        sns.subscribe(TopicArn=topic, Protocol="email", Endpoint=a.email)
        print("  confirmation e-mail sent to", a.email)

    cw.put_metric_alarm(AlarmName="uhi-high-intensity", Namespace="UHI", MetricName="MaxIntensity",
                        Statistic="Maximum", Period=3600, EvaluationPeriods=1,
                        Threshold=config.DEFAULT_ALERT_THRESHOLD, ComparisonOperator="GreaterThanThreshold",
                        AlarmActions=[topic], TreatMissingData="notBreaching")
    print("CloudWatch alarm uhi-high-intensity ->", TOPIC)
    print("\nDone. Set CLOUD_MODE=aws in .env so the console mirrors new objects / alerts live.")


if __name__ == "__main__":
    main()
