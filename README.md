# Solar Cell and Battery Scripts

Scripts and notebooks for working with batteries and solar cells in spacecraft design. Forked from [patrickyeon/battery-and-solar-cell-testing](https://github.com/patrickyeon/battery-and-solar-cell-testing) (Open Lunar Foundation) and extended for CubeSat EPS bench testing at [ASU SDSL](https://sdsl.asu.edu).

## notebooks/
`sys design.ipynb` is a quick example of sizing battery packs vs solar arrays.
`Orbiter Power.ipynb` is a bit more look at a notional orbiter specifically.
`Solar Cells.ipynb` is an in-depth look at predicting cell and array performance with only spec sheet info.
`TVC Sizing.ipynb` is an exploration of the design space for powering a fairly demanding subsystem.

## Hardware Commands

The hardware scripts are exposed as console commands:

```bash
uv run e4350 --help
uv run talke4530 --help
uv run prologix-scan
```

## E4350 Device Support

- **Models**: E4350A and E4350B (vendor strings may appear as HEWLETT-PACKARD, AGILENT, or KEYSIGHT). The instrument ID (`*IDN?`) can include option codes (for example `J06`) which change voltage/current limits. The script `pyscripts/e4350.py` detects model and common `Jxx` options automatically.

- **Limits & Overrides**: Models and options determine maximum open-circuit voltage (Voc) and short-circuit current (Isc). If detection fails or you prefer to set them manually, use the CLI flags `--max-voltage` and `--max-current` when running the `e4350` command.

- **`--sim` arguments are absolute amps and volts**, not fractions of full scale. The order is `Isc[A],Vmp[V],Imp[A],Voc[V]`. With `--multiple NsMp` the four values are treated as a single cell/string and scaled by N (series) and M (parallel) before being sent.

Dry run with no hardware (realistic 8s1p CubeSat panel values):

```bash
uv run e4350 --nohardware -p /dev/null -a 1 --sim=0.5,18.8,0.48,20.8
```

Real hardware — single 8s1p triple-junction panel, E4350B auto-detected:

```bash
uv run e4350 --sim=0.5,18.8,0.48,20.8 --verbose
```

Two parallel strings from per-cell parameters (8s2p):

```bash
uv run e4350 --multiple 8s2p --sim=0.5,2.35,0.48,2.6 --verbose
```

- **Datasheets & Manuals**:
	- Keysight E4350B/E4351B Operating Guide: https://www.keysight.com/us/en/assets/9018-01168/user-manuals/9018-01168.pdf
	- Agilent E4350B datasheet (METAF): https://www.metaf.com/wp-content/uploads/2022/11/AGILENT-E4350BSOLAR-ARRAY-SIMULATOR-METAF-DATASHEET.pdf
	- Keysight/Agilent E4350A datasheet: https://www.testequipmenthq.com/datasheets/Keysight-E4350A-Datasheet.pdf
	- Agilent E4350B datasheet (TestEquipmentHQ): https://www.testequipmenthq.com/datasheets/Agilent-E4350B-Datasheet.pdf
	- GlobalSpec E4350A datasheet: https://datasheets.globalspec.com/ds/valuetronics-international/e4350a/3381c974-391d-4b67-b8da-989d65c7b272

Consult your instrument's front-panel labeling or `*IDN?` response for the exact option code for your unit.

## About

Originally written by patrickyeon while working for the Open Lunar Foundation. Extended by ASU SDSL for CubeSat EPS bench testing (SquidSat and future missions).

This software is MIT Licensed.

## Setup

This repository is packaged as a `uv` project for Python 3.12+:

```bash
uv sync
```

**Serial port permissions**: The Prologix GPIB-USB adapter appears as `/dev/ttyUSB0`. Add your user to the `dialout` group so you can open it without `sudo`:

```bash
sudo usermod -aG dialout $USER   # then log out and back in
```

`prologix-scan` auto-detects the Prologix serial port, then scans GPIB addresses until it finds a responding instrument.

The `ngspice` build step in `install.sh` is still relevant if you need the PySpice-based solar cell models, because that dependency is not handled by `uv` itself.
