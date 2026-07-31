# Diagram 1 - AWS Cloud Architecture

```mermaid
flowchart TB
    subgraph Sources[Approved external data sources]
        S1[Satellite imagery]
        S2[UHI reference data]
        S3[Weather and land-use data]
    end

    subgraph AWS[AWS cloud environment]
        S3Raw[Amazon S3 - raw data]
        ETL[AWS Lambda / AWS Glue - validation and preprocessing]
        S3Processed[Amazon S3 - processed features]
        SM[Amazon SageMaker - training and inference]
        RDS[Amazon RDS - metadata and summaries]
        S3Output[Amazon S3 - prediction maps]
        APIGW[Amazon API Gateway]
        Cognito[Amazon Cognito]
        CW[Amazon CloudWatch]
        SNS[Amazon SNS]
        IAM[AWS IAM]
    end

    S1 --> S3Raw
    S2 --> S3Raw
    S3 --> S3Raw
    S3Raw --> ETL --> S3Processed --> SM
    SM --> S3Output
    SM --> RDS
    S3Output --> APIGW
    RDS --> APIGW
    Cognito --> APIGW
    ETL -. logs .-> CW
    SM -. logs and metrics .-> CW
    APIGW -. access logs .-> CW
    CW --> SNS
    IAM -. least-privilege permissions .-> S3Raw
    IAM -. least-privilege permissions .-> ETL
    IAM -. least-privilege permissions .-> SM
    IAM -. least-privilege permissions .-> RDS
```

## Data-flow explanation

1. Approved external data is placed in the S3 raw-data zone.
2. Lambda or Glue validates inputs, removes invalid records, aligns spatial data, and creates model-ready features.
3. Processed features are stored separately from source data and passed to SageMaker.
4. SageMaker trains or invokes a model and writes map outputs to S3 plus queryable metadata to RDS.
5. API Gateway provides approved output to the future frontend; Cognito controls user access.
6. CloudWatch records operational events; SNS sends configured notifications.
