# Environmental-Seismic-Analysis-Workflow
Hong-Mao Huang, 2026
</p>
Department of Earth Science, University of Colorado Boulder, CO, USA


## Introduction
This workflow supports environmental seismic analysis through dv/v computation with MSNoise (Lecocq et al., 2014).

Please report any bugs you encounter; pull requests for improvements are also welcome. Thanks!

## Features
- dv/v computation

## Usage
1. Set up `config.toml`.
2. Put your waveform files into the SDS archive before running. This package does not download waveform data.
3. Run the workflow:
```bash
python main.py
```

## Configuration
The workflow is controlled through `config.toml`. The only supported stage is `dvv_calculation`; MSNoise settings, including the processing window, channel selectors, dv/v calculation parameters, and filter definitions, are documented directly in the config file.

## Input Waveforms
Waveform download and FDSN discovery are intentionally not included. Users must prepare an SDS archive that MSNoise can scan:

```text
outputs/dvv_calculation/testing/SDS/
```

The SDS archive should use the standard daily SDS layout:

```text
outputs/dvv_calculation/testing/SDS/
└── YYYY/
    └── NET/
        └── STA/
            └── NET.STA.LOC.CHA.D.YYYY.JJJ
```

For example, a daily HHZ file for station `ABC` in network `XX`, empty location code, and Julian day 60 of 2021 would be placed as:

```text
outputs/dvv_calculation/testing/SDS/2021/XX/ABC/XX.ABC..HHZ.D.2021.060
```

Relative paths are resolved from the project root.

## Formulation
*This section is under development. Please refer to the articles listed in the References section for now.*

## References
<p style="padding-left: 2em; text-indent: -2em;">
Lecocq, T., C. Caudron, et F. Brenguier (2014), MSNoise, a Python Package for Monitoring Seismic Velocity Changes Using Ambient Seismic Noise, Seismological Research Letters, 85(3), 715-726, https://doi.org/10.1785/0220130073.
</p>
