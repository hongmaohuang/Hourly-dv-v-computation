from __future__ import annotations

import csv
import fnmatch
import importlib.util
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


DEFAULT_MSNOISE_PROJECT_DIR = "outputs"
DEFAULT_SDS_FOLDER = "SDS"
CHANNEL_METADATA_FILENAME = "channel_metadata.csv"
DATA_STRUCTURE = "SDS"
MSNOISE_DB_TECH = "1"
STATION_COORDINATES = "DEG"
STATION_INSTRUMENT = "INST"


@dataclass(frozen=True)
class ChannelTarget:
    network: str
    station: str
    location: str
    channel: str
    latitude: float
    longitude: float
    elevation: float
    sample_rate: float
    starttime: str
    endtime: str

    @property
    def seed_id(self) -> str:
        location = self.location or "--"
        return f"{self.network}.{self.station}.{location}.{self.channel}"


@dataclass(frozen=True)
class DvvRunResult:
    cadence: str
    ccf_backend: str
    ccf_commands: list[list[str]]
    validation: dict[str, object]
    hourly_validation: dict[str, object]


def load_config(path: str | Path) -> dict:
    config_path = Path(path).expanduser().resolve()
    if config_path.suffix.lower() != ".toml":
        raise ValueError(f"Only TOML config files are supported: {config_path}")

    with config_path.open("rb") as handle:
        cfg = tomllib.load(handle)
    cfg["_config_path"] = str(config_path)
    cfg["_config_dir"] = str(config_path.parent)
    return cfg


def resolve_path(cfg: dict, raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (Path(cfg["_config_dir"]) / path).resolve()


def require_mapping(cfg: dict, key: str) -> dict:
    value = cfg.get(key)
    if not isinstance(value, dict):
        raise KeyError(f"Missing required config object: {key}")
    return value


def require_value(mapping: dict, key: str, context: str) -> object:
    if key not in mapping or mapping[key] in (None, ""):
        raise KeyError(f"Missing required config value: {context}.{key}")
    return mapping[key]


def require_configured(mapping: dict, key: str, context: str) -> object:
    if key not in mapping or mapping[key] is None:
        raise KeyError(f"Missing required config key: {context}.{key}")
    return mapping[key]


def require_bool(mapping: dict, key: str, context: str) -> bool:
    value = require_value(mapping, key, context)
    if not isinstance(value, bool):
        raise TypeError(f"{context}.{key} must be a boolean")
    return value


def _parse_time(value: str) -> datetime:
    clean_value = value.strip()
    if clean_value.endswith("Z"):
        clean_value = clean_value[:-1]
    if "." in clean_value:
        prefix, fraction = clean_value.split(".", 1)
        clean_value = prefix + "." + fraction[:6].ljust(6, "0")
    try:
        return datetime.fromisoformat(clean_value).replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(clean_value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"Unsupported time format: {value}")


def _format_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _date_from_time(value: str) -> str:
    return _parse_time(value).strftime("%Y-%m-%d")


def _optional_config_date(msnoise_cfg: dict, key: str, fallback: str) -> str:
    raw_value = str(msnoise_cfg.get(key) or "").strip()
    if not raw_value:
        return fallback
    return _date_from_time(raw_value)


def _selector_values(selector: object) -> list[str]:
    return [item.strip() for item in str(selector or "*").split(",") if item.strip()]


def _channel_matches_selector(channel: str, selector: object) -> bool:
    values = _selector_values(selector)
    return any(fnmatch.fnmatchcase(channel.upper(), value.upper()) for value in values)


def _location_matches_selector(location: str, selector: object) -> bool:
    values = _selector_values(selector)
    if "*" in values:
        return True
    normalized_location = location or "--"
    return any(value in {location, normalized_location} for value in values)


def _msnoise_station_name(target: ChannelTarget, msnoise_cfg: dict) -> str:
    if not bool(msnoise_cfg.get("split_locations_as_stations", False)):
        return target.station
    location = target.location or "XX"
    return f"{target.station[:3]}{location[:2]}".upper()


def _configured_component_pairs(msnoise_cfg: dict) -> list[str]:
    pairs: list[str] = []
    for key in ("components_to_compute", "components_to_compute_single_station"):
        raw_value = str(msnoise_cfg.get(key) or "")
        for item in raw_value.split(","):
            pair = item.strip().upper()
            if pair:
                pairs.append(pair)
    return sorted(set(pairs))


def _validate_component_pairs(msnoise_cfg: dict, targets: list[ChannelTarget]) -> None:
    component_pairs = _configured_component_pairs(msnoise_cfg)
    if not component_pairs:
        raise ValueError(
            "No MSNoise components are configured. Set components_to_compute "
            "or components_to_compute_single_station."
        )

    unsupported_pairs = [pair for pair in component_pairs if len(pair) != 2 or set(pair) - set("ZEN12")]
    if unsupported_pairs:
        raise ValueError(
            "Unsupported MSNoise component pair(s) for this workflow: "
            + ", ".join(unsupported_pairs)
            + ". MSNoise compute_cc is reliable here for channel component letters "
            "Z/E/N/1/2. For channels such as HSF whose component letter is F, "
            "compute those data in a separate backend or provide precomputed keep_all CCF HDF5 files."
        )

    available_components = {target.channel[-1].upper() for target in targets if target.channel}
    required_components = set("".join(component_pairs))
    missing_components = sorted(required_components - available_components)
    if missing_components:
        raise RuntimeError(
            "Configured component pair(s) require channel component(s) not present "
            "in the selected SDS files: "
            + ", ".join(missing_components)
            + f". Available components: {', '.join(sorted(available_components)) or 'none'}. "
            "Adjust channels/components_to_compute for this instrument family."
        )


def _validate_location_handling(msnoise_cfg: dict, targets: list[ChannelTarget]) -> None:
    locations_by_station: dict[tuple[str, str], set[str]] = {}
    for target in targets:
        locations_by_station.setdefault((target.network, target.station), set()).add(target.location or "")
    mixed_location_stations = {
        f"{network}.{station}": sorted(location or "--" for location in locations)
        for (network, station), locations in locations_by_station.items()
        if len(locations) > 1
    }
    if mixed_location_stations and not bool(msnoise_cfg.get("split_locations_as_stations", False)):
        examples = "; ".join(
            f"{station}={','.join(locations)}"
            for station, locations in list(sorted(mixed_location_stations.items()))[:5]
        )
        raise RuntimeError(
            "Selected SDS files include multiple location codes for the same station, "
            "but split_locations_as_stations is false. This can mix physically distinct "
            "channels inside one MSNoise station. Set split_locations_as_stations = true "
            f"or restrict location to one code. Examples: {examples}"
        )


def _target_sampling_rate(msnoise_cfg: dict) -> float:
    return float(require_value(msnoise_cfg, "cc_sampling_rate", "dvv_calculation.msnoise"))


def _normalize_trace_sampling(trace, target_sampling_rate: float) -> None:
    current_sampling_rate = float(trace.stats.sampling_rate)
    if abs(current_sampling_rate - target_sampling_rate) < 1e-6:
        trace.data = trace.data.astype("float32")
        return
    if current_sampling_rate < target_sampling_rate:
        trace.interpolate(sampling_rate=target_sampling_rate, method="linear")
        trace.data = trace.data.astype("float32")
        return
    ratio = current_sampling_rate / target_sampling_rate
    rounded_ratio = round(ratio)
    if rounded_ratio > 1 and abs(ratio - rounded_ratio) < 1e-6:
        trace.decimate(int(rounded_ratio), strict_length=False, no_filter=False)
        trace.data = trace.data.astype("float32")
        return
    trace.resample(target_sampling_rate)
    trace.data = trace.data.astype("float32")


def _parse_sds_filename(file_path: Path) -> dict[str, str] | None:
    parts = file_path.name.split(".")
    if len(parts) != 7:
        return None
    net, sta, loc, cha, data_type, year, julian_day = parts
    if not year.isdigit() or not julian_day.isdigit():
        return None
    return {
        "network": net,
        "station": sta,
        "location": loc,
        "channel": cha,
        "data_type": data_type,
        "year": year,
        "julian_day": julian_day,
    }


def _sds_file_matches_config(file_path: Path, msnoise_cfg: dict) -> bool:
    parsed = _parse_sds_filename(file_path)
    if not parsed:
        return False
    data_type = str(msnoise_cfg.get("data_type") or "D")
    if parsed["data_type"] != data_type:
        return False
    return (
        _channel_matches_selector(parsed["channel"], msnoise_cfg.get("channels", "*"))
        and _location_matches_selector(parsed["location"], msnoise_cfg.get("location", "*"))
    )


def _normalize_existing_sds_files(sds_root: Path, target_sampling_rate: float, msnoise_cfg: dict | None = None) -> None:
    from obspy import read

    repaired = 0
    for file_path in sorted(sds_root.rglob("*")):
        if not file_path.is_file():
            continue
        if msnoise_cfg is not None and not _sds_file_matches_config(file_path, msnoise_cfg):
            continue
        stream = read(str(file_path))
        original_rates = [float(trace.stats.sampling_rate) for trace in stream]
        for trace in stream:
            _normalize_trace_sampling(trace, target_sampling_rate)
        stream.merge(method=1, fill_value="interpolate")
        normalized_rates = [float(trace.stats.sampling_rate) for trace in stream]
        if original_rates == normalized_rates and len(original_rates) == len(stream):
            continue
        tmp_path = file_path.with_name(file_path.name + ".tmp")
        if tmp_path.exists():
            tmp_path.unlink()
        stream.write(str(tmp_path), format="MSEED")
        tmp_path.replace(file_path)
        repaired += 1
    if repaired:
        print(f"Normalized {repaired} existing SDS file(s) to {target_sampling_rate:g} Hz.", flush=True)


def _write_channel_metadata(project_dir: Path, targets: list[ChannelTarget], msnoise_cfg: dict) -> None:
    metadata_path = project_dir / CHANNEL_METADATA_FILENAME
    with metadata_path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "network",
                "station",
                "location",
                "channel",
                "msnoise_station",
                "latitude",
                "longitude",
                "elevation",
                "original_sample_rate",
                "target_sample_rate",
                "starttime",
                "endtime",
            ],
        )
        writer.writeheader()
        for target in targets:
            writer.writerow(
                {
                    "network": target.network,
                    "station": target.station,
                    "location": target.location,
                    "channel": target.channel,
                    "msnoise_station": _msnoise_station_name(target, msnoise_cfg),
                    "latitude": target.latitude,
                    "longitude": target.longitude,
                    "elevation": target.elevation,
                    "original_sample_rate": target.sample_rate,
                    "target_sample_rate": _target_sampling_rate(msnoise_cfg),
                    "starttime": target.starttime,
                    "endtime": target.endtime,
                }
            )


def _existing_sds_channel_targets(msnoise_cfg: dict, project_dir: Path) -> list[ChannelTarget]:
    sds_root = _sds_root(msnoise_cfg, project_dir)
    if not sds_root.exists():
        raise FileNotFoundError(f"Existing SDS directory does not exist: {sds_root}")

    by_seed: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for file_path in sorted(sds_root.rglob("*")):
        if not file_path.is_file() or not _sds_file_matches_config(file_path, msnoise_cfg):
            continue
        parsed = _parse_sds_filename(file_path)
        if not parsed:
            continue
        key = (parsed["network"], parsed["station"], parsed["location"], parsed["channel"])
        year = int(parsed["year"])
        julian_day = int(parsed["julian_day"])
        try:
            day = datetime.strptime(f"{year} {julian_day:03d}", "%Y %j").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        start = _format_time(day)
        end = _format_time(day + timedelta(days=1))
        if key not in by_seed:
            by_seed[key] = {
                "starttime": start,
                "endtime": end,
                "files": 1,
            }
            continue
        by_seed[key]["starttime"] = min(str(by_seed[key]["starttime"]), start)
        by_seed[key]["endtime"] = max(str(by_seed[key]["endtime"]), end)
        by_seed[key]["files"] = int(by_seed[key]["files"]) + 1

    targets = [
        ChannelTarget(
            network=net,
            station=sta,
            location=loc,
            channel=cha,
            latitude=0.0,
            longitude=0.0,
            elevation=0.0,
            sample_rate=0.0,
            starttime=str(metadata["starttime"]),
            endtime=str(metadata["endtime"]),
        )
        for (net, sta, loc, cha), metadata in sorted(by_seed.items())
    ]
    if not targets:
        raise RuntimeError(
            "No existing SDS files matched the configured selectors. "
            f"SDS root={sds_root}, channels={msnoise_cfg.get('channels')}, location={msnoise_cfg.get('location')}"
        )
    return targets


def _station_rows_from_targets(targets: list[ChannelTarget], msnoise_cfg: dict) -> list[dict[str, object]]:
    station_rows: dict[tuple[str, str], dict[str, object]] = {}
    for target in targets:
        msnoise_station = _msnoise_station_name(target, msnoise_cfg)
        station_rows[(target.network, msnoise_station)] = {
            "net": target.network,
            "sta": msnoise_station,
            "lon": target.longitude,
            "lat": target.latitude,
            "elev": target.elevation,
        }
    return [station_rows[key] for key in sorted(station_rows)]


def _prepare_existing_sds_project(msnoise_cfg: dict, project_dir: Path) -> tuple[Path, list[dict[str, object]]]:
    sds_root = _sds_root(msnoise_cfg, project_dir)
    targets = _existing_sds_channel_targets(msnoise_cfg, project_dir)
    _validate_component_pairs(msnoise_cfg, targets)
    _validate_location_handling(msnoise_cfg, targets)
    _write_channel_metadata(project_dir, targets, msnoise_cfg)
    station_rows = _station_rows_from_targets(targets, msnoise_cfg)

    print("Using existing SDS archive; waveform download is disabled.", flush=True)
    print(f"Existing SDS channel(s) admitted: {len(targets)}", flush=True)
    print(f"Existing SDS station(s) admitted: {len(station_rows)}", flush=True)
    print("WARNING: Existing-SDS mode derives station coordinates from SDS filenames only; coordinates are set to 0.0.", flush=True)
    return sds_root, station_rows


def _set_msnoise_config(cursor, name: str, value: object) -> None:
    cursor.execute("INSERT OR REPLACE INTO config (name, value) VALUES (?, ?)", (name, str(value)))


def _hourly_cfg(cfg: dict) -> dict:
    dvv_cfg = require_mapping(cfg, "dvv_calculation")
    hourly_cfg = dvv_cfg.get("hourly", {})
    if isinstance(hourly_cfg, dict):
        return hourly_cfg
    return {}


def _output_cfg(cfg: dict) -> dict:
    dvv_cfg = require_mapping(cfg, "dvv_calculation")
    output_cfg = dvv_cfg.get("output", {})
    if isinstance(output_cfg, dict):
        return output_cfg
    return {}


def _output_cadence(cfg: dict) -> str:
    cadence = str(_output_cfg(cfg).get("cadence", "hourly")).strip().lower()
    if cadence not in {"hourly", "daily"}:
        raise ValueError("dvv_calculation.output.cadence must be 'hourly' or 'daily'")
    return cadence


def _hourly_enabled(cfg: dict) -> bool:
    return _output_cadence(cfg) == "hourly"


def _commands_for_cadence(cfg: dict, msnoise_cfg: dict) -> list[list[str]]:
    cadence = _output_cadence(cfg)
    raw_commands = _output_cfg(cfg).get(f"{cadence}_commands")
    if raw_commands is None:
        raw_commands = msnoise_cfg.get("commands")
    if not isinstance(raw_commands, list) or not raw_commands:
        raise ValueError(
            f"dvv_calculation.output.{cadence}_commands must contain at least one command"
        )
    commands: list[list[str]] = []
    for command in raw_commands:
        if not isinstance(command, list) or not command:
            raise ValueError(f"Invalid command in {cadence}_commands: {command!r}")
        commands.append([str(part) for part in command])
    return commands


def _ccf_cfg(cfg: dict) -> dict:
    dvv_cfg = require_mapping(cfg, "dvv_calculation")
    ccf_cfg = dvv_cfg.get("ccf", {})
    if isinstance(ccf_cfg, dict):
        return ccf_cfg
    return {}


def _ccf_backend(cfg: dict) -> str:
    return str(_ccf_cfg(cfg).get("backend", "msnoise")).strip().lower()


def _msnoise_project_dir(cfg: dict, msnoise_cfg: dict | None = None) -> Path:
    if msnoise_cfg is None:
        dvv_cfg = require_mapping(cfg, "dvv_calculation")
        raw_msnoise_cfg = dvv_cfg.get("msnoise")
        if not isinstance(raw_msnoise_cfg, dict):
            raise KeyError("Missing required config object: dvv_calculation.msnoise")
        msnoise_cfg = raw_msnoise_cfg
    return resolve_path(cfg, msnoise_cfg.get("project_dir", DEFAULT_MSNOISE_PROJECT_DIR))


def _sds_root(msnoise_cfg: dict, project_dir: Path) -> Path:
    path = Path(str(msnoise_cfg.get("sds_folder", DEFAULT_SDS_FOLDER))).expanduser()
    if path.is_absolute():
        return path
    return (project_dir / path).resolve()


def _load_hourly_module():
    module_path = Path(__file__).with_name("hourly.py")
    spec = importlib.util.spec_from_file_location("hourly_dvv_runner", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load hourly dv/v runner from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_native_ccf_module():
    module_path = Path(__file__).with_name("native_ccf.py")
    spec = importlib.util.spec_from_file_location("native_scalar_ccf_runner", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load native scalar CCF runner from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_msnoise_processing_config(cursor, msnoise_cfg: dict) -> None:
    startdate = str(msnoise_cfg.get("msnoise_startdate") or _date_from_time(str(require_value(msnoise_cfg, "start_date", "dvv_calculation.msnoise"))))
    enddate = str(msnoise_cfg.get("msnoise_enddate") or _date_from_time(str(require_value(msnoise_cfg, "end_date", "dvv_calculation.msnoise"))))
    startdate = _date_from_time(startdate)
    enddate = _date_from_time(enddate)
    ref_begin = _optional_config_date(msnoise_cfg, "ref_begin", startdate)
    ref_end = _optional_config_date(msnoise_cfg, "ref_end", enddate)
    if ref_end < ref_begin:
        raise ValueError(f"dvv_calculation.msnoise.ref_end ({ref_end}) is earlier than ref_begin ({ref_begin})")
    _set_msnoise_config(cursor, "startdate", startdate)
    _set_msnoise_config(cursor, "enddate", enddate)
    _set_msnoise_config(cursor, "ref_begin", ref_begin)
    _set_msnoise_config(cursor, "ref_end", ref_end)
    _set_msnoise_config(cursor, "autocorr", require_configured(msnoise_cfg, "autocorr", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "components_to_compute", require_value(msnoise_cfg, "components_to_compute", "dvv_calculation.msnoise"))
    _set_msnoise_config(
        cursor,
        "components_to_compute_single_station",
        require_value(msnoise_cfg, "components_to_compute_single_station", "dvv_calculation.msnoise"),
    )
    _set_msnoise_config(cursor, "mov_stack", require_value(msnoise_cfg, "mov_stack", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "analysis_duration", require_value(msnoise_cfg, "analysis_duration", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "corr_duration", require_value(msnoise_cfg, "corr_duration", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "cc_sampling_rate", require_value(msnoise_cfg, "cc_sampling_rate", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "whitening", require_value(msnoise_cfg, "whitening", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "dtt_lag", require_value(msnoise_cfg, "dtt_lag", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "dtt_v", require_value(msnoise_cfg, "dtt_v", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "dtt_width", require_value(msnoise_cfg, "dtt_width", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "dtt_sides", require_value(msnoise_cfg, "dtt_sides", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "dtt_minlag", require_value(msnoise_cfg, "dtt_minlag", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "dtt_mincoh", require_value(msnoise_cfg, "dtt_mincoh", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "dtt_maxerr", require_value(msnoise_cfg, "dtt_maxerr", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "dtt_maxdt", require_value(msnoise_cfg, "dtt_maxdt", "dvv_calculation.msnoise"))
    _set_msnoise_config(cursor, "stack_method", require_value(msnoise_cfg, "stack_method", "dvv_calculation.msnoise"))

    extra_config = require_mapping(msnoise_cfg, "extra_msnoise_config")
    for name, value in extra_config.items():
        _set_msnoise_config(cursor, str(name), value)

    cursor.execute("DELETE FROM filters")
    for filter_cfg in require_value(msnoise_cfg, "filters", "dvv_calculation.msnoise"):
        cursor.execute(
            """
            INSERT INTO filters (ref, low, mwcs_low, high, mwcs_high, rms_threshold, mwcs_wlen, mwcs_step, used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                filter_cfg["ref"],
                filter_cfg["low"],
                filter_cfg["mwcs_low"],
                filter_cfg["high"],
                filter_cfg["mwcs_high"],
                filter_cfg["rms_threshold"],
                filter_cfg["mwcs_wlen"],
                filter_cfg["mwcs_step"],
            ),
        )


def sync_msnoise_project_config(msnoise_cfg: dict, project_dir: Path) -> None:
    db_path = project_dir / "msnoise.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"MSNoise database does not exist: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        _set_msnoise_config(cursor, "data_folder", str(project_dir))
        _set_msnoise_config(cursor, "data_structure", DATA_STRUCTURE)
        _set_msnoise_config(cursor, "data_type", require_value(msnoise_cfg, "data_type", "dvv_calculation.msnoise"))
        _write_msnoise_processing_config(cursor, msnoise_cfg)
        conn.commit()
    finally:
        conn.close()


def clear_msnoise_jobs(project_dir: Path) -> None:
    db_path = project_dir / "msnoise.sqlite"
    if not db_path.exists():
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM jobs")
        conn.commit()
    finally:
        conn.close()


def sync_cadence_msnoise_config(cfg: dict, project_dir: Path) -> None:
    db_path = project_dir / "msnoise.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"MSNoise database does not exist: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        if _output_cadence(cfg) == "hourly":
            hourly_cfg = _hourly_cfg(cfg)
            _set_msnoise_config(cursor, "keep_all", "Y")
            keep_days = "Y" if bool(hourly_cfg.get("keep_daily_cc", False)) else "N"
            _set_msnoise_config(cursor, "keep_days", keep_days)
            _set_msnoise_config(cursor, "output_folder", hourly_cfg.get("source_folder", "CROSS_CORRELATIONS"))
        else:
            _set_msnoise_config(cursor, "keep_all", "N")
            _set_msnoise_config(cursor, "keep_days", "Y")
        conn.commit()
    finally:
        conn.close()


def _write_msnoise_station_table(msnoise_cfg: dict, project_dir: Path, station_rows: list[dict[str, object]]) -> None:
    db_path = project_dir / "msnoise.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"MSNoise database was not initialized: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        _set_msnoise_config(cursor, "data_folder", str(project_dir))
        _set_msnoise_config(cursor, "data_structure", DATA_STRUCTURE)
        _set_msnoise_config(cursor, "data_type", require_value(msnoise_cfg, "data_type", "dvv_calculation.msnoise"))
        _write_msnoise_processing_config(cursor, msnoise_cfg)

        cursor.execute("DELETE FROM stations")
        for row in station_rows:
            cursor.execute(
                """
                INSERT INTO stations (net, sta, X, Y, altitude, coordinates, instrument, used)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (row["net"], row["sta"], row["lon"], row["lat"], row["elev"], STATION_COORDINATES, STATION_INSTRUMENT),
            )

        conn.commit()
    finally:
        conn.close()


def _clear_msnoise_data_availability(project_dir: Path) -> None:
    db_path = project_dir / "msnoise.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"MSNoise database was not initialized: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM data_availability")
        conn.commit()
    finally:
        conn.close()


def _run_msnoise_scan_archive(project_dir: Path, sds_root: Path) -> None:
    command = ["msnoise", "scan_archive", "--path", str(sds_root), "--recursively", "--init"]
    result = subprocess.run(command, cwd=project_dir, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        print("MSNoise scan_archive --init completed.", flush=True)
        return

    combined_output = f"{result.stdout}\n{result.stderr}".lower()
    if "--init" not in combined_output and "no such option" not in combined_output and "unrecognized" not in combined_output:
        raise RuntimeError(
            "MSNoise scan_archive failed: "
            f"command={' '.join(command)}, stdout={result.stdout.strip()}, stderr={result.stderr.strip()}"
        )

    fallback_command = ["msnoise", "scan_archive", "--path", str(sds_root), "--recursively"]
    fallback = subprocess.run(fallback_command, cwd=project_dir, check=False, capture_output=True, text=True)
    if fallback.returncode != 0:
        raise RuntimeError(
            "MSNoise scan_archive failed: "
            f"command={' '.join(fallback_command)}, stdout={fallback.stdout.strip()}, stderr={fallback.stderr.strip()}"
        )
    print("MSNoise scan_archive completed.", flush=True)


def _scan_msnoise_project(msnoise_cfg: dict, project_dir: Path, sds_root: Path, station_rows: list[dict[str, object]]) -> None:
    if bool(msnoise_cfg.get("normalize_existing_sds", False)):
        _normalize_existing_sds_files(sds_root, _target_sampling_rate(msnoise_cfg), msnoise_cfg)
    _write_msnoise_station_table(msnoise_cfg, project_dir, station_rows)
    _clear_msnoise_data_availability(project_dir)
    _run_msnoise_scan_archive(project_dir, sds_root)


def run_msnoise_project_setup(msnoise_cfg: dict, project_dir: Path) -> None:
    if not require_bool(msnoise_cfg, "prepare_project", "dvv_calculation.msnoise"):
        return

    if require_bool(msnoise_cfg, "reset_project_dir", "dvv_calculation.msnoise"):
        raise ValueError(
            "dvv_calculation.msnoise.reset_project_dir must be false because waveform downloading "
            "has been removed; provide an existing SDS archive instead."
        )

    project_dir.mkdir(parents=True, exist_ok=True)
    sds_root, station_rows = _prepare_existing_sds_project(msnoise_cfg, project_dir)
    if not (project_dir / "msnoise.sqlite").exists():
        subprocess.run(["msnoise", "db", "init", "--tech", MSNOISE_DB_TECH], cwd=project_dir, check=True)
    _scan_msnoise_project(msnoise_cfg, project_dir, sds_root, station_rows)


def validate_msnoise_outputs(cfg: dict, project_dir: Path) -> dict[str, object]:
    if _ccf_backend(cfg) != "msnoise":
        return {}

    dvv_cfg = require_mapping(cfg, "dvv_calculation")
    msnoise_cfg = dvv_cfg.get("msnoise")
    if not isinstance(msnoise_cfg, dict) or not require_bool(msnoise_cfg, "validate_outputs", "dvv_calculation.msnoise"):
        return {}
    if not require_bool(msnoise_cfg, "run_commands", "dvv_calculation.msnoise"):
        return {}

    ref_files = sorted((project_dir / "STACKS").glob("*/REF/*/*"))
    mwcs_files = sorted((project_dir / "MWCS").glob("*/*/*/*/*.txt"))
    dtt_files = sorted((project_dir / "DTT").glob("*/*/*/*.txt"))
    validation = {
        "ref_files": len(ref_files),
        "mwcs_files": len(mwcs_files),
        "dtt_files": len(dtt_files),
        "first_dtt_file": dtt_files[0] if dtt_files else None,
    }

    missing: list[str] = []
    daily_required = (
        _output_cadence(cfg) == "daily"
        and bool(_output_cfg(cfg).get("require_outputs", True))
    )
    if (daily_required or require_bool(msnoise_cfg, "require_ref", "dvv_calculation.msnoise")) and not ref_files:
        missing.append("REF stacks")
    if (daily_required or require_bool(msnoise_cfg, "require_mwcs", "dvv_calculation.msnoise")) and not mwcs_files:
        missing.append("MWCS files")
    if (daily_required or require_bool(msnoise_cfg, "require_dtt", "dvv_calculation.msnoise")) and not dtt_files:
        missing.append("DTT files")
    if missing:
        raise RuntimeError(
            "MSNoise output validation failed. Missing: "
            + ", ".join(missing)
            + f". Project directory: {project_dir}"
        )

    db_path = project_dir / "msnoise.sqlite"
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "select jobtype, flag, count(*) from jobs group by jobtype, flag order by jobtype, flag"
            )
            validation["job_summary"] = ["|".join(str(item) for item in row) for row in cursor.fetchall()]
        finally:
            conn.close()

    return validation


def run_ccf_backend(cfg: dict) -> tuple[str, list[list[str]], dict[str, object]]:
    dvv_cfg = require_mapping(cfg, "dvv_calculation")
    msnoise_cfg = dvv_cfg.get("msnoise")
    if not isinstance(msnoise_cfg, dict):
        raise KeyError("Missing required config object: dvv_calculation.msnoise")

    backend = _ccf_backend(cfg)
    if backend not in {"msnoise", "native_scalar"}:
        raise ValueError("dvv_calculation.ccf.backend must be 'msnoise' or 'native_scalar'")

    cadence = _output_cadence(cfg)
    if cadence == "daily" and backend != "msnoise":
        raise ValueError(
            "Daily cadence must use dvv_calculation.ccf.backend='msnoise' so STACK, MWCS, "
            "and DTT are computed directly by MSNoise."
        )

    project_dir = _msnoise_project_dir(cfg, msnoise_cfg)
    normalized_commands = _commands_for_cadence(cfg, msnoise_cfg)
    if not require_bool(msnoise_cfg, "run_commands", "dvv_calculation.msnoise"):
        if backend == "native_scalar":
            return backend, [["native_scalar_ccf"]], {}
        return backend, normalized_commands, {}

    if backend == "native_scalar":
        project_dir.mkdir(parents=True, exist_ok=True)
        native_ccf = _load_native_ccf_module()
        native_result = native_ccf.run_native_scalar_ccf(cfg, project_dir)
        return backend, [["native_scalar_ccf"]], {
            "native_targets": native_result.targets,
            "native_pairs": native_result.pairs,
            "native_ccf_files": native_result.ccf_files,
            "native_ccf_windows": native_result.ccf_windows,
            "native_skipped_files": native_result.skipped_files,
            "native_skipped_windows": native_result.skipped_windows,
            "native_source_root": native_result.source_root,
            "native_manifest": native_result.manifest_path,
        }

    run_msnoise_project_setup(msnoise_cfg, project_dir)
    sync_msnoise_project_config(msnoise_cfg, project_dir)
    sync_cadence_msnoise_config(cfg, project_dir)
    if require_bool(msnoise_cfg, "clear_jobs_on_run", "dvv_calculation.msnoise"):
        clear_msnoise_jobs(project_dir)

    for command in normalized_commands:
        subprocess.run(command, cwd=project_dir, check=True)

    return backend, normalized_commands, {}


def run_msnoise_backend(cfg: dict) -> list[list[str]]:
    if _ccf_backend(cfg) != "msnoise":
        raise RuntimeError("run_msnoise_backend was called while dvv_calculation.ccf.backend is not 'msnoise'")
    backend, commands, _validation = run_ccf_backend(cfg)
    return commands


def run_dvv_calculation(cfg: dict) -> DvvRunResult:
    dvv_cfg = require_mapping(cfg, "dvv_calculation")
    msnoise_cfg = dvv_cfg.get("msnoise")
    if not isinstance(msnoise_cfg, dict):
        raise KeyError("Missing required config object: dvv_calculation.msnoise")

    ccf_backend, ccf_commands, ccf_validation = run_ccf_backend(cfg)
    project_dir = _msnoise_project_dir(cfg, msnoise_cfg)
    validation = validate_msnoise_outputs(cfg, project_dir)
    validation.update(ccf_validation)
    hourly_validation: dict[str, object] = {}
    if _hourly_enabled(cfg) and require_bool(msnoise_cfg, "run_commands", "dvv_calculation.msnoise"):
        hourly = _load_hourly_module()
        hourly_result = hourly.run_hourly_dvv(cfg, project_dir)
        hourly_validation = {
            "source_files": hourly_result.source_files,
            "stack_files": hourly_result.stack_files,
            "mwcs_files": hourly_result.mwcs_files,
            "dtt_rows": hourly_result.dtt_rows,
            "output_csv": hourly_result.output_csv,
        }

    return DvvRunResult(
        cadence=_output_cadence(cfg),
        ccf_backend=ccf_backend,
        ccf_commands=ccf_commands,
        validation=validation,
        hourly_validation=hourly_validation,
    )


def print_result(result: DvvRunResult) -> None:
    print(f"Result cadence: {result.cadence}")
    print(f"CCF backend: {result.ccf_backend}")
    if result.ccf_commands:
        print("CCF steps configured:")
        for command in result.ccf_commands:
            print("  " + " ".join(command))
    if result.validation:
        print("Validation:")
        if result.validation.get("native_targets") is not None:
            print(f"  Native targets: {result.validation['native_targets']}")
        if result.validation.get("native_pairs") is not None:
            print(f"  Native pairs: {result.validation['native_pairs']}")
        if result.validation.get("native_ccf_files") is not None:
            print(f"  Native CCF files: {result.validation['native_ccf_files']}")
        if result.validation.get("native_ccf_windows") is not None:
            print(f"  Native CCF windows: {result.validation['native_ccf_windows']}")
        if result.validation.get("native_skipped_files") is not None:
            print(f"  Native skipped files: {result.validation['native_skipped_files']}")
        if result.validation.get("native_skipped_windows") is not None:
            print(f"  Native skipped windows: {result.validation['native_skipped_windows']}")
        if result.validation.get("native_source_root"):
            print(f"  Native CCF source: {result.validation['native_source_root']}")
        if result.validation.get("native_manifest"):
            print(f"  Native manifest: {result.validation['native_manifest']}")
        if result.validation.get("ref_files") is not None:
            print(f"  REF files: {result.validation['ref_files']}")
        if result.validation.get("mwcs_files") is not None:
            print(f"  MWCS files: {result.validation['mwcs_files']}")
        if result.validation.get("dtt_files") is not None:
            print(f"  DTT files: {result.validation['dtt_files']}")
        if result.validation.get("first_dtt_file"):
            print(f"  First DTT file: {result.validation['first_dtt_file']}")
        if result.validation.get("job_summary"):
            print("  Job summary:")
            for row in result.validation["job_summary"]:
                print(f"    {row}")
    if result.hourly_validation:
        print("Hourly dv/v validation:")
        print(f"  Source CCF files: {result.hourly_validation['source_files']}")
        print(f"  Hourly stack files: {result.hourly_validation['stack_files']}")
        print(f"  Hourly MWCS files: {result.hourly_validation['mwcs_files']}")
        print(f"  Hourly DTT rows: {result.hourly_validation['dtt_rows']}")
        if result.hourly_validation.get("output_csv"):
            print(f"  Hourly dv/v CSV: {result.hourly_validation['output_csv']}")
