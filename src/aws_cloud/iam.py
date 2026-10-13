"""
AWS IAM emulation - least-privilege roles and policy evaluation.

Service roles (Lambda, Glue, SageMaker) and Cognito user-group roles are defined
as IAM-style JSON policy documents. ``is_allowed`` evaluates Allow/Deny statements
with wildcard matching, exactly like IAM: explicit Deny > Allow > implicit deny.
"""
import fnmatch

from src import config

B = config.S3_BUCKET
A = f"{config.AWS_REGION}:{config.AWS_ACCOUNT_ID}"

ROLES = {
    "UHI-LambdaExecutionRole": {
        "Description": "Validate uploads and trigger ETL / alerts",
        "Statements": [
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": [f"arn:aws:s3:::{B}/raw/*", f"arn:aws:s3:::{B}/logs/*"]},
            {"Effect": "Allow", "Action": ["glue:StartJobRun"], "Resource": ["*"]},
            {"Effect": "Allow", "Action": ["sns:Publish"], "Resource": [f"arn:aws:sns:{A}:uhi-*"]},
            {"Effect": "Allow", "Action": ["logs:*"], "Resource": ["*"]},
        ],
    },
    "UHI-GlueServiceRole": {
        "Description": "ETL: read raw zone, write processed zone",
        "Statements": [
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"], "Resource": [f"arn:aws:s3:::{B}/raw/*"]},
            {"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": [f"arn:aws:s3:::{B}/processed/*"]},
            {"Effect": "Deny", "Action": ["s3:DeleteObject"], "Resource": [f"arn:aws:s3:::{B}/raw/*"]},
            {"Effect": "Allow", "Action": ["logs:*"], "Resource": ["*"]},
        ],
    },
    "UHI-SageMakerExecutionRole": {
        "Description": "Train / host models; read processed data, write artifacts and predictions",
        "Statements": [
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{B}/processed/*", f"arn:aws:s3:::{B}/model-artifacts/*"]},
            {"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": [f"arn:aws:s3:::{B}/model-artifacts/*", f"arn:aws:s3:::{B}/predictions/*"]},
            {"Effect": "Allow", "Action": ["rds-data:ExecuteStatement"], "Resource": ["*"]},
            {"Effect": "Allow", "Action": ["logs:*", "cloudwatch:PutMetricData"], "Resource": ["*"]},
        ],
    },
    "UHI-Admin": {
        "Description": "Cognito group 'admin' - full platform access",
        "Statements": [{"Effect": "Allow", "Action": ["*"], "Resource": ["*"]}],
    },
    "UHI-Analyst": {
        "Description": "Cognito group 'analyst' - run pipelines, train models, predict",
        "Statements": [
            {"Effect": "Allow", "Action": ["s3:*", "glue:*", "sagemaker:*", "states:*", "lambda:*", "cloudwatch:*",
                                           "logs:*", "uhi:Predict", "uhi:Read", "sns:*", "rds:Query", "apigateway:ManageOwnKeys"],
             "Resource": ["*"]},
            {"Effect": "Deny", "Action": ["iam:*", "cognito:AdminUser*"], "Resource": ["*"]},
        ],
    },
    "UHI-Viewer": {
        "Description": "Cognito group 'viewer' - read-only dashboard and predictions",
        "Statements": [
            {"Effect": "Allow", "Action": ["uhi:Read", "uhi:Predict", "s3:GetObject", "s3:ListBucket",
                                           "cloudwatch:Get*", "sns:List*", "apigateway:ManageOwnKeys"], "Resource": ["*"]},
        ],
    },
}

GROUP_ROLE = {"admin": "UHI-Admin", "analyst": "UHI-Analyst", "viewer": "UHI-Viewer"}


def _match(patterns, value):
    return any(fnmatch.fnmatchcase(value, p) for p in patterns)


def is_allowed(role: str, action: str, resource: str = "*") -> bool:
    policy = ROLES.get(role)
    if not policy:
        return False
    allowed = False
    for st in policy["Statements"]:
        if _match(st["Action"], action) and _match(st["Resource"], resource):
            if st["Effect"] == "Deny":
                return False
            allowed = True
    return allowed


def group_allowed(group: str, action: str, resource: str = "*") -> bool:
    return is_allowed(GROUP_ROLE.get(group, ""), action, resource)


def list_roles():
    return [{"RoleName": name, "Arn": f"arn:aws:iam::{config.AWS_ACCOUNT_ID}:role/{name}",
             "Description": r["Description"], "PolicyDocument": {"Version": "2012-10-17", "Statement": r["Statements"]}}
            for name, r in ROLES.items()]
