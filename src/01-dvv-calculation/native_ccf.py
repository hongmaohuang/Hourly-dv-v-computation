from __future__ import annotations

import csv
import fnmatch
import io
import itertools
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.fft as sf
from scipy.fft import next_fast_len


DEFAULT_SDS_FOLDER = "SDS"


@dataclass(frozen=True)
class NativeCcfResult:
    targets: int
    pairs: int
    ccf_files: int
    ccf_windows: int
    skipped_files: int
    skipped_windows: int
    source_root: Path
    manifest_path: Path


@dataclass(frozen=True)
class SdsTarget:
    network: str
    station: str
    location: str
    channel: str
    files_by_day: dict[datetime, Path]

    @property
    def component(self) -> str:
        return self.channel[-1:].upper()

    @property
    def label(self) -> str:
        location = self.location or "--"
        return f"{self.network}.{self.station}.{location}.{self.channel}"


@dataclass(frozen=True)
class CcfPair:
    target1: SdsTarget
    target2: SdsTarget
    components: str

    @property
    def label(self) -> str:
        return f"{self.target1.label}|{self.target2.label}|{self.components}"


@dataclass(frozen=True)
class MsnoiseCcfSettings:
    sampling_rate: float
    corr_duration: float
    overlap: float
    maxlag: float
    windsorizing: float
    whitening: str
    whitening_type: str
    cc_type: str
    cc_type_single_station_ac: str
    cc_type_single_station_sc: str
    preprocess_highpass: float
    preprocess_lowpass: float
    preprocess_max_gap: float
    preprocess_taper_length: float
    resampling_method: str
    remove_response: bool
    archive_format: str


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
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


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
    if parsed["data_type"] != str(msnoise_cfg.get("data_type") or "D"):
        return False
    return (
        _channel_matches_selector(parsed["channel"], msnoise_cfg.get("channels", "*"))
        and _location_matches_selector(parsed["location"], msnoise_cfg.get("location", "*"))
    )


def _discover_targets(msnoise_cfg: dict, sds_root: Path, start: datetime, end: datetime) -> list[SdsTarget]:
    if not sds_root.exists():
        raise FileNotFoundError(f"SDS archive does not exist: {sds_root}")

    by_seed: dict[tuple[str, str, str, str], dict[datetime, Path]] = {}
    for file_path in sorted(sds_root.rglob("*")):
        if not file_path.is_file() or not _sds_file_matches_config(file_path, msnoise_cfg):
            continue
        parsed = _parse_sds_filename(file_path)
        if not parsed:
            continue
        try:
            day = datetime.strptime(
                f"{int(parsed['year'])} {int(parsed['julian_day']):03d}", "%Y %j"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if day >= end or day + timedelta(days=1) <= start:
            continue
        key = (parsed["network"], parsed["station"], parsed["location"], parsed["channel"])
        by_seed.setdefault(key, {})[day] = file_path

    targets = [
        SdsTarget(net, sta, loc, cha, files_by_day)
        for (net, sta, loc, cha), files_by_day in sorted(by_seed.items())
    ]
    if not targets:
        raise RuntimeError(
            "No SDS files matched the native scalar CCF selectors. "
            f"SDS root={sds_root}, channels={msnoise_cfg.get('channels')}, location={msnoise_cfg.get('location')}"
        )
    return targets


def _component_pairs(msnoise_cfg: dict, key: str) -> list[str]:
    pairs: list[str] = []
    for item in str(msnoise_cfg.get(key) or "").split(","):
        pair = item.strip().upper()
        if not pair:
            continue
        if len(pair) != 2:
            raise ValueError(f"Native scalar CCF component pair must contain two letters: {pair}")
        pairs.append(pair)
    return sorted(set(pairs))


def _build_pairs(targets: list[SdsTarget], msnoise_cfg: dict, native_cfg: dict) -> list[CcfPair]:
    single_station_components = _component_pairs(msnoise_cfg, "components_to_compute_single_station")
    cross_station_components = _component_pairs(msnoise_cfg, "components_to_compute")
    if not single_station_components and not cross_station_components:
        raise ValueError("No components are configured for native scalar CCF.")

    targets_by_site_component: dict[tuple[str, str, str, str], list[SdsTarget]] = {}
    for target in targets:
        site_component = (target.network, target.station, target.location, target.component)
        targets_by_site_component.setdefault(site_component, []).append(target)

    ambiguous = {
        key: values
        for key, values in targets_by_site_component.items()
        if len(values) > 1
    }
    if ambiguous:
        labels = ", ".join("/".join(target.label for target in values) for values in ambiguous.values())
        raise RuntimeError(
            "Native scalar CCF found multiple selected channels with the same site and final component. "
            "MSNoise would select the first matching trace, which is ambiguous. Restrict channels so each "
            f"NET.STA.LOC/component has one channel: {labels}"
        )

    pairs: list[CcfPair] = []
    compute_autocorr = _as_bool(native_cfg.get("compute_autocorr"), True)
    compute_cross_pairs = _as_bool(native_cfg.get("compute_cross_pairs"), False)
    max_station_pairs = int(native_cfg.get("max_station_pairs", 0) or 0)
    max_unbounded_cross_pairs = int(native_cfg.get("max_unbounded_cross_pairs", 1000) or 1000)
    allow_unbounded_cross_pairs = _as_bool(native_cfg.get("allow_unbounded_cross_pairs"), False)
    ordered_targets = sorted(targets, key=lambda target: target.label)

    if compute_autocorr:
        for target1, target2 in itertools.combinations_with_replacement(ordered_targets, 2):
            site1 = (target1.network, target1.station, target1.location)
            site2 = (target2.network, target2.station, target2.location)
            if site1 != site2:
                continue
            components = target1.component + target2.component
            if components in single_station_components:
                pairs.append(CcfPair(target1, target2, components))
            reverse_components = components[::-1]
            if target1.component != target2.component and reverse_components in single_station_components:
                pairs.append(CcfPair(target2, target1, reverse_components))

    cross_pairs: list[CcfPair] = []
    if compute_cross_pairs:
        for target1, target2 in itertools.combinations(ordered_targets, 2):
            site1 = (target1.network, target1.station, target1.location)
            site2 = (target2.network, target2.station, target2.location)
            if site1 == site2:
                continue
            components = target1.component + target2.component
            if components in cross_station_components:
                cross_pairs.append(CcfPair(target1, target2, components))

        estimated_cross_pairs = len(cross_pairs)
        if (
            max_station_pairs <= 0
            and estimated_cross_pairs > max_unbounded_cross_pairs
            and not allow_unbounded_cross_pairs
        ):
            raise RuntimeError(
                "Native scalar cross-pair expansion would create "
                f"{estimated_cross_pairs} station pairs. Set max_station_pairs, "
                "increase max_unbounded_cross_pairs, or set allow_unbounded_cross_pairs=true "
                "after testing on a short window."
            )
        if max_station_pairs > 0:
            cross_pairs = cross_pairs[:max_station_pairs]
        pairs.extend(cross_pairs)

    if not pairs:
        available = ", ".join(sorted({target.component for target in targets})) or "none"
        raise RuntimeError(
            "Native scalar CCF found no computable pairs. "
            f"Configured single-station pairs: {', '.join(single_station_components) or 'none'}; "
            f"cross-station pairs: {', '.join(cross_station_components) or 'none'}. "
            f"Available components: {available}."
        )
    unique = {pair.label: pair for pair in pairs}
    return [unique[label] for label in sorted(unique)]


def _msnoise_default_value(key: str) -> object:
    try:
        from msnoise.default import default
    except Exception as exc:  # pragma: no cover - depends on runtime env
        raise RuntimeError("Native scalar CCF requires MSNoise to be importable") from exc

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


def _load_msnoise_ccf_settings(msnoise_cfg: dict, hourly_cfg: dict) -> MsnoiseCcfSettings:
    maxlag = float(_msnoise_setting(msnoise_cfg, "maxlag"))
    if "maxlag" in hourly_cfg and not np.isclose(float(hourly_cfg["maxlag"]), maxlag):
        raise ValueError(
            "dvv_calculation.hourly.maxlag must equal MSNoise maxlag. "
            "Use dvv_calculation.msnoise.extra_msnoise_config.maxlag as the single source of truth."
        )
    settings = MsnoiseCcfSettings(
        sampling_rate=float(_msnoise_setting(msnoise_cfg, "cc_sampling_rate")),
        corr_duration=float(_msnoise_setting(msnoise_cfg, "corr_duration")),
        overlap=float(_msnoise_setting(msnoise_cfg, "overlap")),
        maxlag=maxlag,
        windsorizing=float(_msnoise_setting(msnoise_cfg, "windsorizing")),
        whitening=str(_msnoise_setting(msnoise_cfg, "whitening")).strip().upper(),
        whitening_type=str(_msnoise_setting(msnoise_cfg, "whitening_type")).strip().upper(),
        cc_type=str(_msnoise_setting(msnoise_cfg, "cc_type")).strip().upper(),
        cc_type_single_station_ac=str(
            _msnoise_setting(msnoise_cfg, "cc_type_single_station_AC")
        ).strip().upper(),
        cc_type_single_station_sc=str(
            _msnoise_setting(msnoise_cfg, "cc_type_single_station_SC")
        ).strip().upper(),
        preprocess_highpass=float(_msnoise_setting(msnoise_cfg, "preprocess_highpass")),
        preprocess_lowpass=float(_msnoise_setting(msnoise_cfg, "preprocess_lowpass")),
        preprocess_max_gap=float(_msnoise_setting(msnoise_cfg, "preprocess_max_gap")),
        preprocess_taper_length=float(_msnoise_setting(msnoise_cfg, "preprocess_taper_length")),
        resampling_method=str(_msnoise_setting(msnoise_cfg, "resampling_method")),
        remove_response=_as_bool(_msnoise_setting(msnoise_cfg, "remove_response")),
        archive_format=str(_msnoise_setting(msnoise_cfg, "archive_format") or ""),
    )
    if settings.corr_duration <= 0:
        raise ValueError("MSNoise corr_duration must be positive")
    if not 0 <= settings.overlap < 1:
        raise ValueError("MSNoise overlap must satisfy 0 <= overlap < 1")
    if settings.whitening not in {"A", "N"}:
        raise ValueError("MSNoise compute_cc supports whitening='A' or 'N'")
    if settings.whitening_type not in {"B", "PSD"}:
        raise ValueError("MSNoise whitening_type must be 'B' or 'PSD'")
    if settings.remove_response:
        raise ValueError(
            "native_scalar cannot reproduce MSNoise response removal without the MSNoise response database. "
            "Set remove_response=false or use backend='msnoise'."
        )
    return settings


def _select_target_stream(stream, target: SdsTarget):
    return stream.select(
        network=target.network,
        station=target.station,
        location=target.location,
        channel=target.channel,
    )


def _read_and_preprocess_stream(
    file_path: Path,
    target: SdsTarget,
    day: datetime,
    settings: MsnoiseCcfSettings,
):
    from obspy import UTCDateTime, Stream, read
    from msnoise.api import check_and_phase_shift, getGaps

    stream = read(
        str(file_path),
        starttime=UTCDateTime(day),
        endtime=UTCDateTime(day) + 86400,
        format=settings.archive_format or None,
    )
    stream = _select_target_stream(stream, target).copy()
    if not stream:
        return Stream()

    # MSNoise performs an in-memory MiniSEED round-trip before gap handling.
    buffer = io.BytesIO()
    stream.write(buffer, format="MSEED")
    buffer.seek(0)
    stream = read(buffer, format="MSEED")
    stream.sort()

    for index, trace in enumerate(stream):
        trace.data = trace.data.astype(float)
        trace.stats.network = trace.stats.network.upper()
        trace.stats.station = trace.stats.station.upper()
        trace.stats.channel = trace.stats.channel.upper()
        stream[index] = check_and_phase_shift(trace, settings.preprocess_taper_length)

    if getGaps(stream):
        max_gap_samples = settings.preprocess_max_gap * stream[0].stats.sampling_rate
        gaps = getGaps(stream)
        while gaps:
            too_long = 0
            for gap in gaps:
                if int(gap[-1]) <= max_gap_samples:
                    try:
                        stream[gap[0]] = stream[gap[0]].__add__(
                            stream[gap[1]], method=1, fill_value="interpolate"
                        )
                        stream.remove(stream[gap[1]])
                    except Exception:
                        stream.remove(stream[gap[1]])
                    break
                too_long += 1
            if too_long == len(gaps):
                break
            gaps = getGaps(stream)

    stream = stream.split()
    for trace in list(stream):
        if trace.stats.sampling_rate < settings.sampling_rate - 1:
            stream.remove(trace)

    for trace in list(stream):
        if trace.stats.npts < 4 * settings.preprocess_taper_length * trace.stats.sampling_rate:
            stream.remove(trace)
            continue
        trace.detrend(type="demean")
        trace.detrend(type="linear")
        trace.taper(max_percentage=None, max_length=settings.preprocess_taper_length)

    for trace in stream:
        trace.filter(
            "highpass",
            freq=settings.preprocess_highpass,
            zerophase=True,
            corners=4,
        )
        if trace.stats.sampling_rate != settings.sampling_rate:
            trace.filter(
                "lowpass",
                freq=settings.preprocess_lowpass,
                zerophase=True,
                corners=8,
            )
            method = settings.resampling_method.lower()
            if method == "resample":
                try:
                    from scikits.samplerate import resample
                except Exception as exc:
                    raise RuntimeError("MSNoise resampling_method='Resample' requires scikits.samplerate") from exc
                trace.data = resample(
                    trace.data,
                    settings.sampling_rate / trace.stats.sampling_rate,
                    "sinc_fastest",
                )
            elif method == "decimate":
                factor = trace.stats.sampling_rate / settings.sampling_rate
                if int(factor) != factor:
                    raise ValueError(
                        f"{trace.id} cannot be decimated by an integer factor from "
                        f"{trace.stats.sampling_rate:g} to {settings.sampling_rate:g} Hz"
                    )
                trace.data = trace.data[::int(factor)]
            elif method == "lanczos":
                trace.data = np.asarray(trace.data)
                trace.interpolate(method="lanczos", sampling_rate=settings.sampling_rate, a=1.0)
            else:
                raise ValueError(f"Unsupported MSNoise resampling_method: {settings.resampling_method}")
            trace.stats.sampling_rate = settings.sampling_rate
        trace.data = trace.data.astype(np.float32)
    return stream


def _window_trace(stream, target: SdsTarget):
    selected = _select_target_stream(stream, target)
    return selected[0] if selected else None


def _iter_msnoise_windows(stream1, stream2, pair: CcfPair, settings: MsnoiseCcfSettings):
    from scipy.stats import scoreatpercentile

    autocorrelation = pair.target1.label == pair.target2.label
    current = stream1 if autocorrelation else stream1 + stream2
    step = settings.corr_duration * (1 - settings.overlap)
    for window in current.slide(settings.corr_duration, step):
        window = window.copy().sort()
        timestamp = window[0].stats.starttime.datetime.replace(tzinfo=timezone.utc)
        gaps = [gap for gap in window.get_gaps(min_gap=0) if gap[-2] > 0]
        base = max(trace.stats.npts for trace in window)
        if gaps or base <= settings.maxlag * settings.sampling_rate * 2 + 1:
            yield timestamp, None, None
            continue
        for trace in list(window):
            if trace.stats.npts != base:
                window.remove(trace)
        required_traces = 1 if autocorrelation else 2
        if len(window) < required_traces:
            yield timestamp, None, None
            continue

        window.detrend("demean")
        for trace in window:
            if settings.windsorizing == -1:
                np.sign(trace.data, trace.data)
            elif settings.windsorizing != 0:
                minimum, maximum = scoreatpercentile(trace.data, [1, 99])
                not_outliers = np.where((trace.data >= minimum) & (trace.data <= maximum))[0]
                rms = trace.data[not_outliers].std() * settings.windsorizing
                np.clip(trace.data, -rms, rms, trace.data)
        window.taper(0.04)

        trace1 = _window_trace(window, pair.target1)
        trace2 = trace1 if pair.target1.label == pair.target2.label else _window_trace(window, pair.target2)
        if trace1 is None or trace2 is None:
            yield timestamp, None, None
            continue
        yield timestamp, trace1, trace2


def _compute_msnoise_ccf(
    trace1,
    trace2,
    pair: CcfPair,
    filter_cfg: dict,
    settings: MsnoiseCcfSettings,
) -> np.ndarray | None:
    import matplotlib.mlab as mlab
    from msnoise.move2obspy import myCorr2, pcc_xcorr, whiten2
    from obspy.signal.filter import bandpass

    low = float(filter_cfg["low"])
    high = float(filter_cfg["high"])
    autocorrelation = pair.target1.label == pair.target2.label and pair.components[0] == pair.components[1]
    same_station = (
        pair.target1.network,
        pair.target1.station,
        pair.target1.location,
    ) == (
        pair.target2.network,
        pair.target2.station,
        pair.target2.location,
    )
    traces = (trace1,) if autocorrelation else (trace1, trace2)
    nfft = next_fast_len(trace1.stats.npts)
    data = np.asarray([trace.data for trace in traces])
    processed = data.copy()
    if settings.whitening == "N":
        for index, values in enumerate(processed):
            processed[index] = bandpass(
                values,
                freqmin=low,
                freqmax=high,
                df=settings.sampling_rate,
                corners=8,
            )

    index = [[pair.label, 0, 0 if autocorrelation else 1]]
    if autocorrelation:
        if settings.whitening == "A":
            for position, values in enumerate(processed):
                processed[position] = bandpass(
                    values,
                    freqmin=low,
                    freqmax=high,
                    df=settings.sampling_rate,
                    corners=8,
                )
        if settings.cc_type_single_station_ac == "CC":
            ffts = sf.fftn(processed, [nfft], axes=[1])
            energy = np.real(
                np.sqrt(np.mean(sf.ifft(ffts, n=nfft, axis=1) ** 2, axis=1))
            )
            correlations = myCorr2(
                ffts,
                np.ceil(settings.maxlag * settings.sampling_rate),
                energy,
                index,
                plot=False,
                nfft=nfft,
            )
        elif settings.cc_type_single_station_ac == "PCC":
            correlations = pcc_xcorr(
                processed,
                np.ceil(settings.maxlag * settings.sampling_rate),
                None,
                index,
            )
        else:
            raise ValueError(
                f"Unsupported MSNoise cc_type_single_station_AC: {settings.cc_type_single_station_ac}"
            )
    else:
        ccf_type = settings.cc_type_single_station_sc if same_station else settings.cc_type
        if ccf_type != "CC":
            key = "cc_type_single_station_SC" if same_station else "cc_type"
            raise ValueError(f"Unsupported MSNoise {key}: {ccf_type}")
        ffts = sf.fftn(processed, [nfft], axes=[1])
        if settings.whitening != "N":
            frequencies = sf.fftfreq(nfft, d=1.0 / settings.sampling_rate)[:nfft // 2]
            selected = np.where((frequencies >= low) & (frequencies <= high))[0]
            if not len(selected):
                raise ValueError(f"No FFT bins inside correlation filter {low:g}-{high:g} Hz")
            taper_low = max(int(selected[0]) - 100, 0)
            pass_low = int(selected[0])
            pass_high = int(selected[-1])
            taper_high = min(int(selected[-1]) + 100, nfft // 2)
            if settings.whitening_type == "PSD":
                psds = []
                for trace in traces:
                    pxx, _frequencies = mlab.psd(
                        trace.data,
                        Fs=trace.stats.sampling_rate,
                        NFFT=nfft,
                        detrend="mean",
                    )
                    psds.append(np.sqrt(pxx))
                psds = np.asarray(psds)
                if same_station and set(pair.components) == {"E", "N"}:
                    mean_psd = psds.mean(axis=0)
                    psds[:] = mean_psd
            else:
                psds = np.zeros(1)
            whiten2(
                ffts,
                nfft,
                taper_low,
                taper_high,
                pass_low,
                pass_high,
                psds,
                settings.whitening_type,
            )
        energy = np.real(
            np.sqrt(np.mean(sf.ifft(ffts, n=nfft, axis=1) ** 2, axis=1))
        )
        correlations = myCorr2(
            ffts,
            np.ceil(settings.maxlag * settings.sampling_rate),
            energy,
            index,
            plot=False,
            nfft=nfft,
        )

    corr = correlations.get(pair.label)
    if corr is None:
        return None
    expected_samples = int(2 * settings.maxlag * settings.sampling_rate) + 1
    if not np.all(np.isfinite(corr)) or len(corr) < expected_samples:
        return None
    return np.asarray(corr)


def _write_day_frame(output_path: Path, rows: dict[datetime, np.ndarray], lags: np.ndarray) -> int:
    if not rows:
        return 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ordered_times = sorted(rows)
    frame = pd.DataFrame(
        [rows[timestamp] for timestamp in ordered_times],
        index=[pd.Timestamp(timestamp) for timestamp in ordered_times],
        columns=lags,
    )
    frame.to_hdf(output_path, key="data", mode="w")
    return len(frame)


def _clear_source_root(project_dir: Path, sds_root: Path, source_root: Path) -> None:
    resolved_source = source_root.resolve()
    resolved_project = project_dir.resolve()
    resolved_sds = sds_root.resolve()
    if resolved_source in {resolved_project, resolved_sds}:
        raise ValueError(
            "Refusing to clear native CCF output because hourly.source_folder resolves "
            f"to a protected path: {resolved_source}"
        )
    if resolved_source.exists():
        shutil.rmtree(resolved_source)


def _manifest_fieldnames() -> list[str]:
    return [
        "filter",
        "day",
        "station1",
        "station2",
        "components",
        "expected_windows",
        "written_windows",
        "skipped_windows",
        "output_file",
        "status",
        "message",
    ]


def run_native_scalar_ccf(cfg: dict, project_dir: Path) -> NativeCcfResult:
    dvv_cfg = _require_mapping(cfg, "dvv_calculation", "config")
    msnoise_cfg = _require_mapping(dvv_cfg, "msnoise", "dvv_calculation")
    hourly_cfg = _require_mapping(dvv_cfg, "hourly", "dvv_calculation")
    native_cfg = dvv_cfg.get("native_scalar_ccf", {})
    if not isinstance(native_cfg, dict):
        native_cfg = {}

    sds_folder = Path(str(msnoise_cfg.get("sds_folder", DEFAULT_SDS_FOLDER))).expanduser()
    sds_root = sds_folder if sds_folder.is_absolute() else (project_dir / sds_folder).resolve()
    source_root = _resolve_project_path(project_dir, hourly_cfg.get("source_folder", "CROSS_CORRELATIONS"))
    if _as_bool(native_cfg.get("clear_output"), True):
        _clear_source_root(project_dir, sds_root, source_root)

    start = _parse_datetime(msnoise_cfg["start_date"])
    end = _parse_datetime(msnoise_cfg["end_date"], end_bound=True)
    if end <= start:
        raise ValueError("dvv_calculation.msnoise.end_date must be later than start_date")

    settings = _load_msnoise_ccf_settings(msnoise_cfg, hourly_cfg)
    lag_samples = int(2 * settings.maxlag * settings.sampling_rate) + 1
    lags = np.linspace(-settings.maxlag, settings.maxlag, lag_samples)
    progress_interval = max(int(native_cfg.get("progress_interval_pairs", 10) or 10), 1)
    bad_waveform_behavior = str(native_cfg.get("bad_waveform_behavior", "warn_skip")).strip().lower()
    if bad_waveform_behavior not in {"fail", "warn_skip"}:
        raise ValueError("native_scalar_ccf.bad_waveform_behavior must be 'fail' or 'warn_skip'")

    targets = _discover_targets(msnoise_cfg, sds_root, start, end)
    pairs = _build_pairs(targets, msnoise_cfg, native_cfg)
    filters = msnoise_cfg.get("filters", [])
    if not filters:
        raise ValueError("Native scalar CCF requires at least one dvv_calculation.msnoise.filters entry.")

    ccf_files = 0
    ccf_windows = 0
    skipped_files = 0
    skipped_windows = 0
    processed_pair_filters = 0
    source_root.mkdir(parents=True, exist_ok=True)
    manifest_path = source_root / "native_ccf_manifest.csv"
    print(
        "Native scalar CCF starting: "
        f"targets={len(targets)}, pairs={len(pairs)}, filters={len(filters)}, "
        f"whitening={settings.whitening}, source={source_root}",
        flush=True,
    )

    with manifest_path.open("w", newline="") as manifest_handle:
        manifest_writer = csv.DictWriter(manifest_handle, fieldnames=_manifest_fieldnames())
        manifest_writer.writeheader()

        for pair in pairs:
            common_days = sorted(set(pair.target1.files_by_day) & set(pair.target2.files_by_day))
            for day in common_days:
                rows_by_filter: dict[int, dict[datetime, np.ndarray]] = {
                    int(filter_cfg["ref"]): {} for filter_cfg in filters
                }
                skipped_by_filter = {int(filter_cfg["ref"]): 0 for filter_cfg in filters}
                messages_by_filter = {int(filter_cfg["ref"]): "" for filter_cfg in filters}
                expected_windows = 0
                try:
                    stream1 = _read_and_preprocess_stream(
                        pair.target1.files_by_day[day], pair.target1, day, settings
                    )
                    stream2 = (
                        stream1
                        if pair.target1.label == pair.target2.label
                        else _read_and_preprocess_stream(
                            pair.target2.files_by_day[day], pair.target2, day, settings
                        )
                    )
                except Exception as exc:
                    if bad_waveform_behavior == "fail":
                        raise RuntimeError(
                            f"Failed preprocessing native scalar CCF waveform for {pair.label} on {day:%Y-%m-%d}"
                        ) from exc
                    skipped_files += 1
                    message = f"{type(exc).__name__}: {exc}"
                    print(
                        "WARNING: skipping native scalar CCF day because MSNoise preprocessing failed: "
                        f"{pair.label} {day:%Y-%m-%d}: {message}",
                        flush=True,
                    )
                    for filter_cfg in filters:
                        filter_id = int(filter_cfg["ref"])
                        filter_name = f"{filter_id:02d}"
                        output_path = source_root / filter_name / pair.target1.label / pair.target2.label / pair.components / f"{day:%Y-%m-%d}.h5"
                        manifest_writer.writerow(
                            {
                                "filter": filter_name,
                                "day": f"{day:%Y-%m-%d}",
                                "station1": pair.target1.label,
                                "station2": pair.target2.label,
                                "components": pair.components,
                                "expected_windows": 0,
                                "written_windows": 0,
                                "skipped_windows": 0,
                                "output_file": output_path,
                                "status": "skipped_file",
                                "message": message,
                            }
                        )
                    continue

                for timestamp, trace1, trace2 in _iter_msnoise_windows(stream1, stream2, pair, settings):
                    expected_windows += 1
                    for filter_cfg in filters:
                        filter_id = int(filter_cfg["ref"])
                        if trace1 is None or trace2 is None:
                            skipped_by_filter[filter_id] += 1
                            continue
                        try:
                            corr = _compute_msnoise_ccf(trace1, trace2, pair, filter_cfg, settings)
                            if corr is None:
                                skipped_by_filter[filter_id] += 1
                                continue
                            rows_by_filter[filter_id][timestamp] = corr
                        except Exception as exc:
                            if bad_waveform_behavior == "fail":
                                raise RuntimeError(
                                    f"Failed native scalar CCF window for {pair.label} at {timestamp.isoformat()}"
                                ) from exc
                            skipped_by_filter[filter_id] += 1
                            messages_by_filter[filter_id] = f"{type(exc).__name__}: {exc}"

                for filter_cfg in filters:
                    filter_id = int(filter_cfg["ref"])
                    filter_name = f"{filter_id:02d}"
                    output_path = source_root / filter_name / pair.target1.label / pair.target2.label / pair.components / f"{day:%Y-%m-%d}.h5"
                    written = _write_day_frame(output_path, rows_by_filter[filter_id], lags)
                    if written:
                        ccf_files += 1
                        ccf_windows += written
                    skipped = skipped_by_filter[filter_id]
                    skipped_windows += skipped
                    manifest_writer.writerow(
                        {
                            "filter": filter_name,
                            "day": f"{day:%Y-%m-%d}",
                            "station1": pair.target1.label,
                            "station2": pair.target2.label,
                            "components": pair.components,
                            "expected_windows": expected_windows,
                            "written_windows": written,
                            "skipped_windows": skipped,
                            "output_file": output_path if written else "",
                            "status": "ok" if written else "no_windows",
                            "message": messages_by_filter[filter_id],
                        }
                    )
                    processed_pair_filters += 1
                    if processed_pair_filters % progress_interval == 0:
                        print(
                            "Native scalar CCF progress: "
                            f"pair_filters={processed_pair_filters}, files={ccf_files}, "
                            f"windows={ccf_windows}, skipped_windows={skipped_windows}",
                            flush=True,
                        )

    if ccf_files == 0:
        raise RuntimeError("Native scalar CCF produced no HDF5 files; check time range, SDS coverage, and filters.")
    print(
        f"Native scalar CCF completed: files={ccf_files}, windows={ccf_windows}, "
        f"skipped_files={skipped_files}, skipped_windows={skipped_windows}, manifest={manifest_path}",
        flush=True,
    )
    return NativeCcfResult(
        targets=len(targets),
        pairs=len(pairs),
        ccf_files=ccf_files,
        ccf_windows=ccf_windows,
        skipped_files=skipped_files,
        skipped_windows=skipped_windows,
        source_root=source_root,
        manifest_path=manifest_path,
    )
