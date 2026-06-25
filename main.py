#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent
DVV_RUNNER = ROOT / "src" / "01-dvv-calculation" / "runner.py"


def load_runner(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load runner from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_config(path: str | Path) -> dict:
    config_path = Path(path).expanduser().resolve()

    with config_path.open("rb") as handle:
        cfg = tomllib.load(handle)
    cfg["_config_path"] = str(config_path)
    cfg["_config_dir"] = str(config_path.parent)
    return cfg


def main() -> int:
    cfg = load_config(ROOT / "config.toml")
    raw_stage = str(cfg.get("workflow", {}).get("active_stage", "dvv_calculation"))
    stages = [stage.strip() for stage in raw_stage.split(",") if stage.strip()]
    if not stages:
        raise ValueError("workflow.active_stage must include at least one stage")

    for stage in stages:
        if stage == "dvv_calculation":
            runner = load_runner("dvv_calculation_runner", DVV_RUNNER)
            result = runner.run_dvv_calculation(cfg)
            runner.print_result(result)
            continue

        raise ValueError(f"Unsupported stage: {stage}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
