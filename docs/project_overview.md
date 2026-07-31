# Project Overview

## Abstract

Urban Heat Island (UHI) is an environmental problem in which built-up urban areas experience higher land surface temperatures than surrounding rural areas. It can increase cooling-energy demand, worsen thermal discomfort, and increase heat-related health risk. Conventional UHI studies often require manual handling of satellite images and disconnected data sources, which makes city-scale analysis slow and difficult to extend. This project proposes a cloud-based UHI prediction system using Geospatial AI. The planned system will combine satellite imagery, land surface temperature, vegetation indices, built-up indicators, land-use/land-cover information, weather variables, and spatial context to estimate UHI intensity and identify heat-prone zones. Amazon S3 will provide scalable storage for raw and processed geospatial data. AWS Lambda and AWS Glue will automate ingestion, cleaning, spatial alignment, and feature preparation. Amazon SageMaker will train, evaluate, and serve prediction models. Amazon RDS will store selected prediction metadata, while Amazon API Gateway and Amazon Cognito will provide controlled future access to prediction results. Amazon CloudWatch and Amazon SNS will support monitoring and high-heat notifications. The proposed framework is designed for reproducibility, secure cloud operation, and future extension to an interactive map-based dashboard. The project is currently in its planning stage; no implementation code is required for Phase I.

## Existing challenges

1. Satellite and geospatial raster files are large and costly to process on personal computers.
2. UHI prediction requires combining heterogeneous data: thermal imagery, vegetation, built-up surfaces, weather, and land-use information.
3. Cloud cover, missing observations, and different spatial resolutions require careful preprocessing.
4. A model can appear accurate while failing to generalise to a new area or time period.
5. A complete solution needs secure storage, repeatable processing, model deployment, monitoring, and visualisation - not only a one-time map.

## Proposed solution

The planned solution is an AWS-based pipeline that stores source data in Amazon S3, preprocesses it using Lambda or Glue, creates model-ready features, trains a regression model in SageMaker, stores resulting map layers and metadata, and exposes approved predictions through an authenticated API. The system will produce UHI-intensity estimates and heat-risk maps for a selected study city.

## Scope and boundary

The target is **satellite-derived surface UHI / land surface temperature**, not a direct substitute for neighbourhood air temperature. The study city and final period will be selected before implementation. Results will be framed as decision-support information, not as medical or emergency warnings.
