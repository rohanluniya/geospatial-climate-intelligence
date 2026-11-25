# ADR-004: Deseasonalized Robust MAD Z-Scores Over Naive Raw-Delta Thresholds

## Status
Accepted

## Context
Naive climate anomaly detectors that compare an observation against an annual mean (`obs - annual_mean`) suffer from severe **seasonal-cycle confounding**: normal summer highs and normal winter lows both trigger false-positive alarms in mid-latitude and Arctic biomes.

## Decision
1. **Harmonic Seasonal Baseline:** Subtract the hemisphere-aware seasonal harmonic expectation anchored to each basin's robust median residual (`np.median`) rather than ordinary mean.
2. **Robust Dispersion Scale:** Estimate baseline variability using Median Absolute Deviation (`1.4826 * MAD`) with physical floors (`sigma_lst >= 1.35 °C`, `sigma_ndvi >= 0.045`, `sigma_ndwi >= 0.055`) so an extreme heatwave or flood pulse cannot mask itself by inflating the sample standard deviation.
3. **Side-by-Side Transparency:** Persist both `deseasonalized_z_*` and `naive_raw_delta_lst_c` plus `naive_seasonal_confounding_fp` in `gold.temporal_anomalies` so operators can verify suppressed false positives directly.

## Scientific Limitations
- A 1-year synthetic record has lower climatological certainty than a 30-year WMO reference period (1991–2020); every zonal and anomaly record explicitly surfaces sample size (`valid_pixels`), 95% confidence half-widths, and caveats.
