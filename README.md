# Environmental-Seismic-Analysis-Workflow
Hong-Mao Huang, 2026
</p>
Department of Earth Science, University of Colorado Boulder, CO, USA


## Introduction
This workflow supports environmental seismic analysis through dv/v computation with MSNoise (Lecocq et al., 2014).

Please report any bugs you encounter; pull requests for improvements are also welcome. Thanks!

## Features
- dv/v computation
- Hourly dv/v computation from MSNoise keep-all CCF windows
- Direct daily dv/v computation through the standard MSNoise pipeline
- Optional native scalar CCF backend for non-standard component letters

## Usage
1. Set up `config.toml`.
2. Put your waveform files into the SDS archive before running. This package does not download waveform data.
3. Run the workflow:
```bash
python main.py
```

## Configuration
The workflow is controlled through `config.toml`. The only supported stage is `dvv_calculation`; MSNoise settings, including the processing window, channel selectors, dv/v calculation parameters, and filter definitions, are documented directly in the config file.

Choose the final cadence with:

```toml
[dvv_calculation.output]
cadence = "hourly" # or "daily"
```

Set `[dvv_calculation.output].cadence = "hourly"` or `"daily"`. Hourly cadence sets MSNoise `keep_all=Y`, runs MSNoise through `compute_cc`, then reads the HDF5 CCF windows and computes hourly STACK, MWCS, and DTT with this package. Daily cadence requires `backend = "msnoise"`, sets `keep_all=N` and `keep_days=Y`, runs the complete native MSNoise STACK/MWCS/DTT command sequence, and does not call `hourly.py`.

For channel families outside MSNoise's guarded component set, set `[dvv_calculation.ccf].backend = "native_scalar"`. This backend generalizes MSNoise's standard no-rotation `compute_cc` component selection while retaining full `NET.STA.LOC.CHA` identity. It calls the same MSNoise/ObsPy preprocessing and correlation operations and writes the same keep-all HDF5 layout. Synthetic HHZ/ZZ autocorrelation and cross-station parity tests, followed by identical HSF/FF tests, produce identical CCF samples in the supported configuration. Numerical identity across different MSNoise, ObsPy, SciPy, FFT, or platform versions is not promised.

Set `[dvv_calculation.msnoise].project_dir` to choose where the MSNoise project and products live. The default is `outputs`. Hourly mode uses `outputs/SDS/`, `outputs/CROSS_CORRELATIONS/`, and `outputs/HOURLY_DVV/`; daily mode writes standard MSNoise `STACKS/`, `MWCS/`, and `DTT/` directories. Set `sds_folder` if your SDS archive is elsewhere; relative paths are resolved from `project_dir`.

For large Alpine runs, keep `write_stack_files = false`, `write_mwcs_files = false`, and `compute_all_pairs = false` unless you are debugging a short test window. Otherwise the workflow can create one intermediate file per hour, filter, component, and station pair. The hourly DTT CSV is streamed to a temporary file and atomically moved into place at the end of a successful run.

Different instrument/channel families should be processed in separate runs by changing `channels`, the component-pair settings, and if needed the CCF backend. Channel selectors such as `EN?`, `HH?`, and `CH?` are fine with the MSNoise backend when the final channel letter is one of `Z/E/N/1/2`, for example `ZZ` for ENZ/HHZ/CHZ. For DAS-style scalar data such as `HSF`, use `channels = "HSF"`, `components_to_compute = "FF"`, `components_to_compute_single_station = "FF"`, and `backend = "native_scalar"`.

The native scalar backend has explicit scaling controls. Keep `compute_cross_pairs = false` for large single-station or DAS autocorrelation runs. Turning it on applies MSNoise's trace-order component-pair rules and expands station pairs roughly as `N^2`; use `max_station_pairs` for short tests before production runs.

Location codes are preserved from SDS filenames. With the MSNoise backend, multiple locations under one `NET.STA` require `split_locations_as_stations = true` or a restricted `location` selector. The native scalar backend keeps each full `NET.STA.LOC.CHA` identity separate and does not require MSNoise station aliases.

## Input Waveforms
Waveform download and FDSN discovery are intentionally not included. Users must prepare an SDS archive that MSNoise can scan:

```text
outputs/SDS/
```

The SDS archive should use the standard daily SDS layout:

```text
outputs/SDS/
└── YYYY/
    └── NET/
        └── STA/
            └── NET.STA.LOC.CHA.D.YYYY.JJJ
```

For example, a daily HHZ file for station `ABC` in network `XX`, empty location code, and Julian day 60 of 2021 would be placed as:

```text
outputs/SDS/2021/XX/ABC/XX.ABC..HHZ.D.2021.060
```

Relative paths are resolved from the project root.

## Hourly dv/v Outputs
Hourly products are written below the MSNoise project directory:

```text
outputs/HOURLY_DVV/
├── HOURLY_STACKS/
├── HOURLY_MWCS/
└── hourly_dvv.csv
```

The combined `hourly_dvv.csv` file includes one row per time, filter, component, and pair, with `M`, `M0`, regression errors, and explicit `dvv = -M` / `dvv0 = -M0` columns. Hourly mode requires `corr_duration = 3600` and `overlap = 0`. Stacking uses MSNoise's stack function, including its moving-window boundary and missing-row behavior; MWCS calls MSNoise's MWCS function. Static DTT uses MSNoise's lag masking, quality selection, `1/error` weights, free-intercept and origin-constrained ObsPy regressions, and `ALL`-pair aggregation.

`HOURLY_STACKS/` and `HOURLY_MWCS/` are only populated when their corresponding `write_*_files` settings are enabled.

## Daily dv/v Outputs

With `cadence = "daily"`, the workflow uses MSNoise directly and writes its standard products below `outputs/STACKS/`, `outputs/MWCS/`, and `outputs/DTT/`. Setting `stack_window_hours = 24` in hourly mode is not equivalent: that produces a 24-hour moving stack updated every hour, not one MSNoise daily result per day.

## Native Scalar CCF
The native scalar CCF backend is intended for future channel/component families that MSNoise's CCF layer does not support cleanly. It:

- preserves full `NET.STA.LOC.CHA` identity in CCF output paths;
- streams one day/filter/pair at a time;
- uses MSNoise's phase alignment, gap interpolation limit, detrend, taper, filters, resampling, windsorizing, whitening, FFT, and correlation functions;
- rejects windows containing unresolved gaps, following MSNoise;
- writes `native_ccf_manifest.csv` with per-day written/skipped window counts;
- clears stale CCF HDF5 files by default with `clear_output = true`;
- writes HDF5 files under `outputs/CROSS_CORRELATIONS/<filter>/<seed1>/<seed2>/<components>/<date>.h5`.

Scientific CCF parameters come only from `[dvv_calculation.msnoise]` and `[dvv_calculation.msnoise.extra_msnoise_config]`; `[dvv_calculation.native_scalar_ccf]` contains execution-safety controls, not an alternative signal-processing recipe. Native scalar intentionally rejects `remove_response = true`, because reproducing MSNoise response removal requires its response database; use `backend = "msnoise"` for that case.

For large production runs, leave `bad_waveform_behavior = "warn_skip"` if you prefer the run to survive a small number of corrupt waveform days, then inspect `native_ccf_manifest.csv` afterward. Use `"fail"` when validating a new dataset and you want the first bad waveform to stop the run.

## Formulation
*This section is under development. Please refer to the articles listed in the References section for now.*

## References
<p style="padding-left: 2em; text-indent: -2em;">
Lecocq, T., C. Caudron, et F. Brenguier (2014), MSNoise, a Python Package for Monitoring Seismic Velocity Changes Using Ambient Seismic Noise, Seismological Research Letters, 85(3), 715-726, https://doi.org/10.1785/0220130073.
</p>
