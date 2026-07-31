# Implementation Roadmap

## Phase I - planning (current)

- Complete abstract, objectives, survey, research gap, data plan, and AWS architecture.
- Create the GitHub planning repository.
- No implementation code is required.

## Phase II - data and baseline

- Select the study city and final temporal range.
- Acquire approved source data and preserve dataset metadata.
- Build data-quality checks and a reproducible preprocessing workflow.
- Create a simple baseline prediction model.

## Phase III - model and cloud workflow

- Build feature-engineering pipeline.
- Train and compare regression models using spatial and temporal validation.
- Record MAE, RMSE, R-squared, model version, and dataset version.
- Store approved outputs in S3 and metadata in RDS.

## Phase IV - deployment and dashboard

- Serve approved prediction results through API Gateway.
- Add authentication through Cognito.
- Build an interactive dashboard with map layers and trend charts.
- Configure CloudWatch dashboards and SNS workflow alerts.

## Definition of done for a future prediction run

1. Dataset source, date, area, and preprocessing parameters are recorded.
2. Missing-data and cloud-mask checks pass.
3. Model is evaluated against an untouched validation subset.
4. Results include MAE, RMSE, R-squared, and a map output.
5. Outputs have no embedded credentials or sensitive data.
