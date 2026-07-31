# Individual Research-Gap Analysis

**Prepared by:** Rohit Kumar (23BIT0030)

## Existing methods

The reviewed work uses remote sensing, GIS mapping, statistical regression, machine learning, deep learning, urban-growth simulation, and multi-source climate modelling. Common input variables are land surface temperature, NDVI, built-up indicators, land-use/land-cover, elevation, and meteorological conditions.

## Advantages of existing work

- Satellite imagery gives repeatable coverage over large geographic areas.
- Vegetation and built-up indices provide meaningful predictors of surface heat patterns.
- Machine-learning models can represent non-linear relationships between urban form and heat.
- Multi-source models can provide more context than temperature data alone.
- Spatial maps make UHI patterns easier to communicate to urban planners.

## Limitations identified through the review

1. Many studies are one-time or locally processed analyses rather than repeatable cloud pipelines.
2. Data sources can differ in spatial resolution, time of capture, coordinate system, and missing-data pattern.
3. Some papers emphasise model complexity without showing how the model will be securely deployed, monitored, and maintained.
4. Reported accuracy is difficult to compare when studies use different target variables and validation procedures.
5. Static UHI maps are valuable but do not automatically provide new predictions after fresh data arrives.
6. Most research papers focus on one city, which may limit transferability to other urban forms and climates.

## Research gap

There is a practical gap for a **reproducible, cloud-native, and prediction-oriented UHI workflow** that integrates multi-source geospatial features; separates raw, processed, model, and output data; evaluates models using documented metrics; deploys approved predictions through controlled access; and monitors the workflow after deployment. The gap is primarily in end-to-end operational design, not in claiming that no UHI prediction research exists.

## Proposed improvement

This project will address the gap through an AWS-based design:

1. Amazon S3 will keep raw, processed, model, and prediction data logically separated.
2. Lambda and/or Glue will automate repeatable data validation and preprocessing steps.
3. SageMaker will compare baseline and advanced regression models using MAE, RMSE, and R-squared.
4. API Gateway and Cognito will support controlled dashboard access rather than public data exposure.
5. CloudWatch will record workflow health, while SNS can notify on job failure or configured high-heat thresholds.
6. Architecture and data versions will be documented so the solution can later be extended from one city to multiple cities.

## Novelty summary

The project novelty is the combination of Geospatial AI with a complete cloud workflow for a student-scale, city-focused use case. It brings together multi-source data integration, automated preprocessing, model evaluation, secure output access, monitoring, and dashboard-ready map products. The goal is a transparent and scalable workflow, not an unsupported claim of a universally superior algorithm.
