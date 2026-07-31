# Dataset Details

## Primary dataset

| Item | Detail |
|---|---|
| Dataset name | Global Urban Heat Island (UHI) Data Set, 2013, Version 1.00 |
| Producer | Center for International Earth Science Information Network (CIESIN), Columbia University; distributed through NASA SEDAC |
| Source URL | https://doi.org/10.7927/H4H70CRF |
| Coverage | Global urban areas; 2013 |
| Data basis | Aqua MODIS MYD11A2 Level-3, 8-day land-surface-temperature composites and GRUMPv1 urban extents |
| Target information | Average summer daytime maximum and nighttime minimum LST for urban and rural areas, plus urban-rural temperature difference using a 10 km rural buffer |
| Data type | Raster geospatial data, associated map services, and metadata |
| Licence / access | Publicly accessible NASA/SEDAC dataset. The official portal terms must be checked again before final download and redistribution. |
| Purpose in this project | Reference UHI layer and context for identifying surface-heat patterns; not a replacement for local air-temperature measurements. |

## Size, records, and features

The dataset is distributed as geographic layers rather than a fixed row-based table. Its exact file size and record count depend on the selected download format, area of interest, resolution, and preprocessing result. To avoid inventing values before acquisition, the implementation will record the following in a `dataset_manifest.csv`:

| Required item | Phase I value / plan |
|---|---|
| Download size | To be measured after selecting the study-city subset and source format. |
| Number of records | To be counted after raster cells or grid samples are converted to a model-ready table. |
| Final feature count | Expected 10-12 predictor/target fields; final count will be written after feature selection. |
| Expected input fields | LST, UHI difference, NDVI, built-up index, land-use class, elevation, albedo, distance to water, urban density, temperature, humidity, wind speed. |

This is the correct Phase I treatment: file size and record count are not stable properties until the project selects its study city, dates, spatial resolution, and final unit of analysis.

## Supporting datasets planned

| Source | Planned purpose |
|---|---|
| Landsat 8/9 Collection 2 Level-2 | Surface temperature and multispectral features for the selected city. |
| Sentinel-2 Level-2A | Higher-resolution vegetation and built-up indicators; it does not provide a thermal band. |
| Land-use / land-cover data | Urban form and surface-category features. |
| Meteorological data | Temperature, humidity, wind speed, and rainfall context. |
| DEM / elevation data | Terrain-related feature. |
| OpenStreetMap or approved urban layers | Road, building, green-space, and water-body context. |

## Planned preprocessing

1. Record source URL, data version, access date, city boundary, and coordinate reference system.
2. Remove corrupt files and flag missing observations; apply satellite quality and cloud masks where available.
3. Reproject and align layers to one selected coordinate system and grid resolution.
4. Create NDVI, built-up, water, and other approved derived indices.
5. Join climate, land-use, and spatial features to a common grid or sample unit.
6. Normalise model features where required and convert approved features to a model-ready table.
7. Use a documented spatial and/or temporal train-validation-test split to reduce leakage.

## Data policy

Raw datasets will not be committed to this repository. The future project will store source metadata and small sample schemas only; managed cloud storage will hold the actual geospatial files.
