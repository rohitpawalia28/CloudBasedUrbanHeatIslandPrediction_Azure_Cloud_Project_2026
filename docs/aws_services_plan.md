# AWS Services Plan

| AWS service | Planned role in this project | Security / operational note |
|---|---|---|
| Amazon S3 | Store raw satellite, weather, vector, processed-feature, and prediction-output files. | Separate raw, processed, and output prefixes; enable encryption and block public access. |
| AWS Lambda | Trigger lightweight validation or preprocessing when new files are uploaded. | Use least-privilege IAM roles and keep functions small. |
| AWS Glue | Run scalable ETL jobs for repeated raster/vector metadata preparation and tabular feature creation. | Store job logs in CloudWatch and keep job parameters versioned. |
| Amazon SageMaker | Train, evaluate, register, and run UHI prediction models. | Use isolated execution roles, experiment tracking, and model metrics before deployment. |
| Amazon RDS | Store project metadata, dataset versions, prediction summaries, and user-facing query records. | Use private access, backups, and parameterised database operations. |
| Amazon API Gateway | Expose selected prediction and metadata endpoints to the dashboard. | Use throttling, request validation, and authenticated access. |
| Amazon Cognito | Authenticate future dashboard users and manage role-based access. | Avoid placing access keys in the frontend. |
| Amazon CloudWatch | Collect logs, alarms, metrics, and service-health observations. | Alert on failed preprocessing, failed predictions, and API errors. |
| Amazon SNS | Send approved alerts for failed workflows or configured high-heat thresholds. | Notification messages will state that outputs are model-based decision support. |
| AWS IAM | Control least-privilege permissions among all services. | No credentials will be committed to GitHub. |
| AWS Step Functions (optional) | Orchestrate multi-step ingestion, preprocessing, training, and prediction workflows. | Useful when implementation needs retry and error-handling visibility. |

## Planned S3 layout

```text
s3://<project-bucket>/
|-- raw/             # Immutable source files and acquisition metadata
|-- processed/       # Cleaned and aligned features
|-- model-artifacts/ # Registered model files and evaluation reports
|-- predictions/     # Generated map layers and summaries
`-- logs/            # Optional pipeline audit exports
```

## Cost-aware Phase I approach

The project will begin with one selected city and a limited date range. Storage lifecycle rules, small development instances, serverless processing where suitable, and manual approval before repeated training jobs will help prevent unnecessary cloud cost.
