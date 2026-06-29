from __future__ import annotations

import csv
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import signal


@dataclass(frozen=True)
class HourlyDvvResult:
    source_files: int
    stack_files: int
    mwcs_files: int
    dtt_rows: int
    output_csv: Path | None


@dataclass(frozen=True)
class HourlyTarget:
    filter_id: int
    station1: str
    station2: str
    components: str
    files: tuple[Path, ...]

    @property
    def pair_name(self) -> str:
        return f"{self.station1.replace('.', '_')}_{self.station2.replace('.', '_')}"


def _require_mapping(mapping: dict, key: str, context: str) -> dict:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise KeyError(f"Missing required config object: {context}.{key}")
    return value


def _parse_datetime(value: object, *, end_bound: bool = False) -> datetime:
    raw = str(value).strip()
    if not raw:
        raise ValueError("Empty datetime value")
    date_only = "T" not in raw and " " not in raw
    clean = raw[:-1] if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(clean)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    if end_bound and date_only:
        parsed = parsed + timedelta(days=1)
    return parsed


def _timestamp_label(value: pd.Timestamp) -> str:
    return value.strftime("%Y-%m-%dT%H-%M-%S")


def _resolve_project_path(project_dir: Path, raw_path: object) -> Path:
    path = Path(str(raw_path)).expanduser()
    if path.is_absolute():
        return path
    return (project_dir / path).resolve()


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _float_config(mapping: dict, key: str, default: float) -> float:
    value = mapping.get(key, default)
    return float(value)


def _int_config(mapping: dict, key: str, default: int) -> int:
    value = mapping.get(key, default)
    return int(value)


def _msnoise_default_value(key: str) -> object:
    try:
        from msnoise.default import default
    except Exception as exc:  # pragma: no cover - depends on runtime env
        raise RuntimeError("Hourly dv/v requires MSNoise to be importable") from exc
    if key not in default:
        raise KeyError(f"MSNoise has no default configuration key named {key}")
    entry = default[key]
    raw_value = entry[1]
    if len(entry) < 3:
        return raw_value
    converter = entry[2]
    if converter is bool:
        return _as_bool(raw_value)
    return converter(raw_value)


def _msnoise_setting(msnoise_cfg: dict, key: str) -> object:
    if key in msnoise_cfg:
        return msnoise_cfg[key]
    extra = msnoise_cfg.get("extra_msnoise_config", {})
    if isinstance(extra, dict) and key in extra:
        return extra[key]
    return _msnoise_default_value(key)


def _discover_targets(source_root: Path) -> list[HourlyTarget]:
    grouped: dict[tuple[int, str, str, str], list[Path]] = {}
    for file_path in sorted(source_root.glob("*/*/*/*/*.h5")):
        rel = file_path.relative_to(source_root)
        if len(rel.parts) != 5:
            continue
        filter_name, station1, station2, components, _filename = rel.parts
        try:
            filter_id = int(filter_name)
        except ValueError:
            continue
        grouped.setdefault((filter_id, station1, station2, components), []).append(file_path)
    return [
        HourlyTarget(filter_id, station1, station2, components, tuple(files))
        for (filter_id, station1, station2, components), files in sorted(grouped.items())
    ]


def _read_ccf_frame(file_path: Path) -> pd.DataFrame:
    frame = pd.read_hdf(file_path, "data")
    if frame.empty:
        return frame
    frame.index = pd.to_datetime(frame.index, utc=True)
    frame.columns = [float(column) for column in frame.columns]
    return frame.reindex(sorted(frame.columns), axis=1)


def _combine_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames).sort_index()
    return combined[~combined.index.duplicated(keep="last")]


def _read_time_filtered_frames(files: tuple[Path, ...], start: datetime, end: datetime) -> list[pd.DataFrame]:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    frames = []
    for file_path in files:
        frame = _read_ccf_frame(file_path)
        if frame.empty:
            continue
        frame = frame[(frame.index >= start_ts) & (frame.index < end_ts)]
        if not frame.empty:
            frames.append(frame)
    return frames


def _ensure_matching_lags(frame: pd.DataFrame, expected_lags: pd.Index, context: str) -> None:
    if len(frame.columns) != len(expected_lags) or not np.allclose(frame.columns.to_numpy(dtype=float), expected_lags.to_numpy(dtype=float)):
        raise RuntimeError(f"CCF lag axis changed while processing {context}; cannot compute hourly dv/v safely.")


def _validate_lag_axis(lags: pd.Index, maxlag: float, sampling_rate: float, context: str) -> None:
    lag_values = lags.to_numpy(dtype=float)
    if len(lag_values) < 2:
        raise RuntimeError(f"CCF lag axis is too short while processing {context}.")
    expected_step = 1.0 / sampling_rate
    actual_steps = np.diff(lag_values)
    if not np.allclose(actual_steps, expected_step, atol=expected_step * 0.01, rtol=0.01):
        raise RuntimeError(
            f"CCF lag axis spacing does not match cc_sampling_rate while processing {context}: "
            f"expected step {expected_step:g}, got median step {np.median(actual_steps):g}."
        )
    inferred_maxlag = max(abs(float(lag_values[0])), abs(float(lag_values[-1])))
    if abs(inferred_maxlag - maxlag) > expected_step:
        raise RuntimeError(
            f"CCF lag axis maxlag does not match MSNoise maxlag while processing {context}: "
            f"configured {maxlag:g}, inferred {inferred_maxlag:g}."
        )


def _stack_matrix(values: np.ndarray, stack_method: str, sampling_rate: float, pws_timegate: float, pws_power: float) -> np.ndarray:
    if stack_method not in {"linear", "pws"}:
        raise ValueError(f"Unsupported hourly stack_method: {stack_method}")
    try:
        from msnoise.api import stack as msnoise_stack
    except Exception as exc:  # pragma: no cover - depends on runtime env
        raise RuntimeError("Hourly stacking requires MSNoise to be importable") from exc

    matrix = np.asarray(values, dtype=float)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    stacked = np.asarray(
        msnoise_stack(matrix.copy(), stack_method, pws_timegate, pws_power, sampling_rate)
    )
    if stacked.size == 0:
        raise RuntimeError("MSNoise rejected every CCF row while computing an hourly stack.")
    return stacked


def _stack_moving_values(
    values: list[np.ndarray],
    *,
    stack_hours: int,
    stack_method: str,
    sampling_rate: float,
    pws_timegate: float,
    pws_power: float,
) -> np.ndarray | None:
    matrix = np.vstack(values)
    if np.all(np.isnan(matrix)):
        return None
    stacked = _stack_matrix(matrix, stack_method, sampling_rate, pws_timegate, pws_power)
    if stack_hours > 1:
        stacked = signal.detrend(stacked).astype(np.float32)
    return stacked


def _iter_ccf_rows(
    files: tuple[Path, ...],
    start: datetime,
    end: datetime,
    expected_lags: pd.Index,
):
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    previous_timestamp = None
    for file_path in files:
        frame = _read_ccf_frame(file_path)
        if frame.empty:
            continue
        _ensure_matching_lags(frame, expected_lags, str(file_path))
        frame = frame[(frame.index >= start_ts) & (frame.index < end_ts)]
        for timestamp, row in frame.iterrows():
            if timestamp.minute or timestamp.second or timestamp.microsecond:
                raise RuntimeError(
                    f"CCF timestamp is not aligned to an hourly grid: {timestamp.isoformat()}"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise RuntimeError("CCF timestamps must be strictly increasing for hourly stacking")
            previous_timestamp = timestamp
            yield timestamp, row.to_numpy(dtype=float)


def _iter_msnoise_moving_stacks(
    files: tuple[Path, ...],
    start: datetime,
    end: datetime,
    expected_lags: pd.Index,
    *,
    stack_hours: int,
    stack_method: str,
    sampling_rate: float,
    pws_timegate: float,
    pws_power: float,
):
    grid = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="1h", inclusive="left")
    if grid.empty:
        return
    rows = iter(_iter_ccf_rows(files, start, end, expected_lags))
    pending = next(rows, None)
    missing = np.full(len(expected_lags), np.nan, dtype=float)

    def value_at(timestamp: pd.Timestamp) -> np.ndarray:
        nonlocal pending
        if pending is not None and pending[0] < timestamp:
            raise RuntimeError(
                f"CCF timestamp {pending[0].isoformat()} does not match the expected hourly grid"
            )
        if pending is not None and pending[0] == timestamp:
            value = pending[1]
            pending = next(rows, None)
            return value
        return missing.copy()

    initial_count = min(stack_hours, len(grid))
    initial_values = [value_at(timestamp) for timestamp in grid[:initial_count]]
    initial_stack = _stack_moving_values(
        initial_values,
        stack_hours=stack_hours,
        stack_method=stack_method,
        sampling_rate=sampling_rate,
        pws_timegate=pws_timegate,
        pws_power=pws_power,
    )
    for timestamp in grid[:initial_count]:
        if initial_stack is not None:
            yield timestamp, initial_stack.copy()

    rolling_values: deque[np.ndarray] = deque(initial_values, maxlen=stack_hours)
    for timestamp in grid[initial_count:]:
        rolling_values.append(value_at(timestamp))
        current = _stack_moving_values(
            list(rolling_values),
            stack_hours=stack_hours,
            stack_method=stack_method,
            sampling_rate=sampling_rate,
            pws_timegate=pws_timegate,
            pws_power=pws_power,
        )
        if current is not None:
            yield timestamp, current

    if pending is not None:
        raise RuntimeError(f"CCF timestamp {pending[0].isoformat()} lies outside the expected hourly grid")


def _write_mseed(path: Path, data: np.ndarray, sampling_rate: float, maxlag: float) -> None:
    from obspy import Stream, Trace, UTCDateTime

    path.parent.mkdir(parents=True, exist_ok=True)
    trace = Trace(data=np.asarray(data, dtype=np.float32))
    trace.stats.sampling_rate = sampling_rate
    trace.stats.starttime = UTCDateTime(1970, 1, 1) - maxlag
    Stream([trace]).write(str(path), format="MSEED")


def _compute_mwcs(current: np.ndarray, reference: np.ndarray, filter_cfg: dict, sampling_rate: float, maxlag: float) -> np.ndarray:
    try:
        from msnoise.move2obspy import mwcs
    except Exception as exc:  # pragma: no cover - depends on runtime env
        raise RuntimeError("Hourly MWCS requires MSNoise to be importable") from exc

    return mwcs(
        current,
        reference,
        float(filter_cfg["mwcs_low"]),
        float(filter_cfg["mwcs_high"]),
        sampling_rate,
        -maxlag,
        float(filter_cfg["mwcs_wlen"]),
        float(filter_cfg["mwcs_step"]),
    )


def _dtt_lag_mask(t: np.ndarray, dtt_cfg: dict) -> np.ndarray:
    if str(dtt_cfg.get("dtt_lag", "static")).lower() != "static":
        raise ValueError("Hourly DTT currently supports dtt_lag='static' only")

    minlag = _float_config(dtt_cfg, "dtt_minlag", 5.0)
    width = _float_config(dtt_cfg, "dtt_width", 30.0)
    sides = str(dtt_cfg.get("dtt_sides", "both")).lower()
    left = (t >= -(minlag + width)) & (t <= -minlag)
    right = (t >= minlag) & (t <= minlag + width)
    if sides == "both":
        return left | right
    if sides == "left":
        return left
    return right


def _mask_mwcs_to_dtt_lag(mwcs_output: np.ndarray, dtt_cfg: dict) -> np.ndarray:
    masked = np.asarray(mwcs_output, dtype=float).copy()
    selected = _dtt_lag_mask(masked[:, 0], dtt_cfg)
    masked[~selected, 2] = 1.0
    masked[~selected, 3] = 0.0
    return masked


def _msnoise_dtt_regressions(
    x: np.ndarray,
    y: np.ndarray,
    errors: np.ndarray,
) -> tuple[float, float, float, float, float, float]:
    try:
        from obspy.signal.regression import linear_regression
    except Exception as exc:  # pragma: no cover - depends on runtime env
        raise RuntimeError("Hourly DTT requires ObsPy to be importable") from exc

    # Match msnoise.s06compute_dtt: the values passed as weights are 1/error,
    # and non-finite weights (including 1/0) are replaced by 1.
    with np.errstate(divide="ignore", invalid="ignore"):
        weights = 1.0 / errors
    weights[~np.isfinite(weights)] = 1.0

    m, a, em, ea = linear_regression(
        x,
        y,
        weights,
        intercept_origin=False,
    )
    m0, em0 = linear_regression(
        x,
        y,
        weights,
        intercept_origin=True,
    )
    return float(m), float(em), float(a), float(ea), float(m0), float(em0)


def _dtt_from_mwcs(
    mwcs_output: np.ndarray,
    *,
    timestamp: pd.Timestamp,
    filter_id: int,
    components: str,
    pair_name: str,
    mov_stack_hours: int,
    dtt_cfg: dict,
) -> dict[str, object] | None:
    if mwcs_output.size == 0:
        return None

    masked_output = _mask_mwcs_to_dtt_lag(mwcs_output, dtt_cfg)
    t = masked_output[:, 0]
    dt = masked_output[:, 1]
    err = masked_output[:, 2]
    coh = masked_output[:, 3]

    mincoh = _float_config(dtt_cfg, "dtt_mincoh", 0.7)
    maxerr = _float_config(dtt_cfg, "dtt_maxerr", 2.0)
    maxdt = _float_config(dtt_cfg, "dtt_maxdt", 2.0)
    quality_mask = (coh >= mincoh) & (err <= maxerr) & (np.abs(dt) <= maxdt)
    finite_mask = np.isfinite(t) & np.isfinite(dt) & np.isfinite(err) & np.isfinite(coh)
    selected = quality_mask & finite_mask
    # MSNoise runs both regressions when at least two MWCS points survive.
    if int(np.count_nonzero(selected)) < 2:
        return None

    x = t[selected]
    y = dt[selected]
    m, em, a, ea, m0, em0 = _msnoise_dtt_regressions(x, y, err[selected].copy())
    if not np.isfinite(m) and not np.isfinite(m0):
        return None
    return {
        "time": timestamp.isoformat(),
        "filter": filter_id,
        "mov_stack_hours": mov_stack_hours,
        "components": components,
        "pair": pair_name,
        "M": m,
        "EM": em,
        "M0": m0,
        "EM0": em0,
        "A": a,
        "EA": ea,
        "dvv": -m if np.isfinite(m) else math.nan,
        "dvv0": -m0 if np.isfinite(m0) else math.nan,
        "n_points": int(np.count_nonzero(selected)),
    }


def _weighted_average(values: np.ndarray, errors: np.ndarray) -> tuple[float, float]:
    # Equivalent to msnoise.s06compute_dtt.wavg_wstd, including NaN uncertainty
    # when fewer than two pair values are available for an ALL-pair lag.
    safe_errors = errors.copy()
    safe_errors[safe_errors == 0] = 1e-6
    with np.errstate(divide="ignore", invalid="ignore"):
        weights = 1.0 / safe_errors
        weight_sum = weights.sum()
        average = (values * weights).sum() / weight_sum
        count = len(np.nonzero(weights)[0])
        variance = np.sum(weights * (values - average) ** 2) / (
            (count - 1) * weight_sum / count
        )
    return float(average), float(np.sqrt(variance))


def _all_pair_mwcs(outputs: list[np.ndarray], dtt_cfg: dict) -> np.ndarray | None:
    if len(outputs) < 2:
        return None
    outputs = [_mask_mwcs_to_dtt_lag(output, dtt_cfg) for output in outputs]
    base_t = outputs[0][:, 0]
    if any(output.shape != outputs[0].shape or not np.allclose(output[:, 0], base_t) for output in outputs[1:]):
        return None

    mincoh = _float_config(dtt_cfg, "dtt_mincoh", 0.7)
    maxerr = _float_config(dtt_cfg, "dtt_maxerr", 2.0)
    maxdt = _float_config(dtt_cfg, "dtt_maxdt", 2.0)
    dt_matrix = np.vstack([output[:, 1] for output in outputs])
    err_matrix = np.vstack([output[:, 2] for output in outputs])
    coh_matrix = np.vstack([output[:, 3] for output in outputs])

    rows = []
    for index, lag in enumerate(base_t):
        valid = (
            np.isfinite(dt_matrix[:, index])
            & np.isfinite(err_matrix[:, index])
            & np.isfinite(coh_matrix[:, index])
            & (coh_matrix[:, index] >= mincoh)
            & (err_matrix[:, index] <= maxerr)
            & (np.abs(dt_matrix[:, index]) <= maxdt)
        )
        average, error = _weighted_average(dt_matrix[:, index][valid], err_matrix[:, index][valid])
        rows.append([lag, average, error, 1.0])
    return np.asarray(rows, dtype=float)


def _csv_fieldnames() -> list[str]:
    return [
        "time",
        "filter",
        "mov_stack_hours",
        "components",
        "pair",
        "M",
        "EM",
        "M0",
        "EM0",
        "A",
        "EA",
        "dvv",
        "dvv0",
        "n_points",
    ]


def run_hourly_dvv(cfg: dict, project_dir: Path) -> HourlyDvvResult:
    dvv_cfg = _require_mapping(cfg, "dvv_calculation", "config")
    msnoise_cfg = _require_mapping(dvv_cfg, "msnoise", "dvv_calculation")
    hourly_cfg = dvv_cfg.get("hourly", {})
    if not isinstance(hourly_cfg, dict):
        raise KeyError("Missing required config object: dvv_calculation.hourly")

    source_root = _resolve_project_path(project_dir, hourly_cfg.get("source_folder", "CROSS_CORRELATIONS"))
    output_root = _resolve_project_path(project_dir, hourly_cfg.get("output_dir", "HOURLY_DVV"))
    stack_root = output_root / "HOURLY_STACKS"
    mwcs_root = output_root / "HOURLY_MWCS"

    stack_hours = _int_config(hourly_cfg, "stack_window_hours", 1)
    if stack_hours < 1:
        raise ValueError("dvv_calculation.hourly.stack_window_hours must be >= 1")
    stack_method = str(_msnoise_setting(msnoise_cfg, "stack_method")).lower()
    write_stack_files = _as_bool(hourly_cfg.get("write_stack_files"), False)
    write_mwcs_files = _as_bool(hourly_cfg.get("write_mwcs_files"), False)
    compute_all_pairs = _as_bool(hourly_cfg.get("compute_all_pairs"), False)
    progress_interval = _int_config(hourly_cfg, "progress_interval_targets", 25)

    sampling_rate = float(msnoise_cfg["cc_sampling_rate"])
    corr_duration = float(_msnoise_setting(msnoise_cfg, "corr_duration"))
    overlap = float(_msnoise_setting(msnoise_cfg, "overlap"))
    if not np.isclose(corr_duration, 3600.0) or not np.isclose(overlap, 0.0):
        raise ValueError(
            "Hourly dv/v requires MSNoise corr_duration=3600 and overlap=0 so each keep-all CCF "
            "maps to exactly one hourly time step."
        )
    maxlag = float(_msnoise_setting(msnoise_cfg, "maxlag"))
    if "maxlag" in hourly_cfg and not np.isclose(float(hourly_cfg["maxlag"]), maxlag):
        raise ValueError(
            "dvv_calculation.hourly.maxlag must equal MSNoise maxlag. "
            "Use dvv_calculation.msnoise.extra_msnoise_config.maxlag as the single source of truth."
        )
    pws_timegate = float(_msnoise_setting(msnoise_cfg, "pws_timegate"))
    pws_power = float(_msnoise_setting(msnoise_cfg, "pws_power"))

    start = _parse_datetime(msnoise_cfg["start_date"])
    end = _parse_datetime(msnoise_cfg["end_date"], end_bound=True)
    ref_begin = _parse_datetime(hourly_cfg.get("reference_begin") or msnoise_cfg.get("ref_begin") or msnoise_cfg["start_date"])
    ref_end = _parse_datetime(hourly_cfg.get("reference_end") or msnoise_cfg.get("ref_end") or msnoise_cfg["end_date"], end_bound=True)

    filters = {int(filter_cfg["ref"]): filter_cfg for filter_cfg in msnoise_cfg["filters"]}
    dtt_cfg = {
        key: hourly_cfg.get(key, msnoise_cfg.get(key))
        for key in ("dtt_lag", "dtt_width", "dtt_sides", "dtt_minlag", "dtt_mincoh", "dtt_maxerr", "dtt_maxdt")
    }
    targets = _discover_targets(source_root)
    if not targets:
        if _as_bool(hourly_cfg.get("require_outputs"), True):
            raise RuntimeError(
                "No keep_all HDF5 CCF files found for hourly dv/v. "
                f"Expected files below: {source_root}"
            )
        return HourlyDvvResult(0, 0, 0, 0, None)

    output_root.mkdir(parents=True, exist_ok=True)
    output_csv = output_root / "hourly_dvv.csv"
    tmp_output_csv = output_root / "hourly_dvv.csv.tmp"
    if tmp_output_csv.exists():
        tmp_output_csv.unlink()

    stack_files = 0
    mwcs_files = 0
    dtt_row_count = 0
    grouped_mwcs: dict[tuple[int, int, str, pd.Timestamp], list[np.ndarray]] = {}

    with tmp_output_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_csv_fieldnames())
        writer.writeheader()

        for target_index, target in enumerate(targets, start=1):
            if progress_interval > 0 and (target_index == 1 or target_index % progress_interval == 0):
                print(f"Hourly dv/v processing target {target_index}/{len(targets)}: {target.pair_name}.{target.components}.{target.filter_id:02d}", flush=True)
            if target.filter_id not in filters:
                continue
            reference_frame = _combine_frames(_read_time_filtered_frames(target.files, ref_begin, ref_end))
            if reference_frame.empty:
                continue
            expected_lags = reference_frame.columns
            _validate_lag_axis(expected_lags, maxlag, sampling_rate, target.pair_name)
            reference = _stack_matrix(reference_frame.to_numpy(), stack_method, sampling_rate, pws_timegate, pws_power)

            moving_stacks = _iter_msnoise_moving_stacks(
                target.files,
                start,
                end,
                expected_lags,
                stack_hours=stack_hours,
                stack_method=stack_method,
                sampling_rate=sampling_rate,
                pws_timegate=pws_timegate,
                pws_power=pws_power,
            )
            for timestamp, current in moving_stacks:
                label = _timestamp_label(timestamp)
                stack_dir = stack_root / f"{target.filter_id:02d}" / f"{stack_hours:03d}_HOURS" / target.components / target.pair_name
                if write_stack_files:
                    _write_mseed(stack_dir / f"{label}.MSEED", current, sampling_rate, maxlag)
                    stack_files += 1

                mwcs_output = _compute_mwcs(current, reference, filters[target.filter_id], sampling_rate, maxlag)
                if write_mwcs_files:
                    mwcs_dir = mwcs_root / f"{target.filter_id:02d}" / f"{stack_hours:03d}_HOURS" / target.components / target.pair_name
                    mwcs_dir.mkdir(parents=True, exist_ok=True)
                    np.savetxt(mwcs_dir / f"{label}.txt", mwcs_output)
                    mwcs_files += 1

                row_data = _dtt_from_mwcs(
                    mwcs_output,
                    timestamp=timestamp,
                    filter_id=target.filter_id,
                    components=target.components,
                    pair_name=target.pair_name,
                    mov_stack_hours=stack_hours,
                    dtt_cfg=dtt_cfg,
                )
                if row_data is not None:
                    writer.writerow(row_data)
                    dtt_row_count += 1
                if compute_all_pairs:
                    grouped_mwcs.setdefault((target.filter_id, stack_hours, target.components, timestamp), []).append(mwcs_output)

        if compute_all_pairs:
            for (filter_id, mov_hours, components, timestamp), outputs in sorted(grouped_mwcs.items(), key=lambda item: item[0]):
                all_output = _all_pair_mwcs(outputs, dtt_cfg)
                if all_output is None:
                    continue
                row_data = _dtt_from_mwcs(
                    all_output,
                    timestamp=timestamp,
                    filter_id=filter_id,
                    components=components,
                    pair_name="ALL",
                    mov_stack_hours=mov_hours,
                    dtt_cfg=dtt_cfg,
                )
                if row_data is not None:
                    writer.writerow(row_data)
                    dtt_row_count += 1

    if dtt_row_count:
        tmp_output_csv.replace(output_csv)
    elif _as_bool(hourly_cfg.get("require_outputs"), True):
        tmp_output_csv.unlink(missing_ok=True)
        raise RuntimeError("Hourly dv/v completed but no DTT rows passed the configured quality thresholds.")
    else:
        tmp_output_csv.unlink(missing_ok=True)
        output_csv = None

    return HourlyDvvResult(
        source_files=sum(len(target.files) for target in targets),
        stack_files=stack_files,
        mwcs_files=mwcs_files,
        dtt_rows=dtt_row_count,
        output_csv=output_csv,
    )
