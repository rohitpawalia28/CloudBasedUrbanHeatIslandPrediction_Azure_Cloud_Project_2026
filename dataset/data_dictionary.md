# Planned Model Data Dictionary

| Field | Type | Description | Role |
|---|---|---|---|
| `grid_id` | string | Unique ID for a spatial cell or sample point. | Identifier |
| `timestamp` | date/time | Acquisition or observation time. | Identifier |
| `latitude`, `longitude` | decimal | Coordinates of the sample centroid. | Spatial context |
| `lst_celsius` | decimal | Satellite-derived land surface temperature in degrees Celsius. | Candidate target / predictor |
| `uhi_intensity_celsius` | decimal | Urban-rural LST difference in degrees Celsius. | Primary target |
| `ndvi` | decimal | Normalized Difference Vegetation Index. | Predictor |
| `ndbi` | decimal | Normalized Difference Built-up Index. | Predictor |
| `ndwi` | decimal | Normalized Difference Water Index. | Predictor |
| `lulc_class` | category | Land-use / land-cover class. | Predictor |
| `elevation_m` | decimal | Elevation in metres. | Predictor |
| `albedo` | decimal | Surface reflectance / albedo measure. | Predictor |
| `distance_to_water_m` | decimal | Distance to closest water body. | Predictor |
| `urban_density` | decimal | Built-up or population-density proxy. | Predictor |
| `air_temperature_celsius` | decimal | Weather-station or reanalysis air temperature. | Predictor |
| `humidity_percent` | decimal | Relative humidity. | Predictor |
| `wind_speed_ms` | decimal | Wind speed in metres per second. | Predictor |

The final data dictionary will be revised only after the selected city, data licence, and feature-engineering workflow are approved.
