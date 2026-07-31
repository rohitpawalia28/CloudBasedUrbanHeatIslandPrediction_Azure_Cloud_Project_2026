# Diagram 2 - Complete System Architecture

```mermaid
flowchart LR
    U[User / planner] --> UI[Future map dashboard]
    UI --> AUTH[Authentication]
    AUTH --> API[Prediction API]

    subgraph Pipeline[Geospatial AI pipeline]
        A[Acquire data] --> B[Store raw data]
        B --> C[Clean, cloud-mask, align, and extract features]
        C --> D[Create model-ready geospatial table]
        D --> E[Train and evaluate model]
        E --> F[Predict UHI intensity]
        F --> G[Create heat-risk map and summary]
    end

    API --> G
    G --> UI
    H[Monitoring and alert rules] --> I[Logs, metrics, and notifications]
    C -. processing events .-> H
    E -. model events .-> H
    API -. service events .-> H
```

## Workflow explanation

1. The student selects an approved city, time range, and data sources.
2. Raw source data is stored without modifying the original files.
3. Preprocessing performs quality checks, cloud masking where relevant, spatial alignment, normalisation, and feature extraction.
4. A model-ready table is created from the spatial features.
5. Baseline and advanced models are trained and compared using MAE, RMSE, and R-squared.
6. The best validated model produces UHI-intensity estimates and heat-risk map layers.
7. The dashboard retrieves only approved outputs through a protected API.
8. Monitoring records pipeline or service failure and can trigger notifications.
