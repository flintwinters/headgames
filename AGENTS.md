# Project guidance

## Motivation

Headgames tests the smallest credible form of direct analog EEG neurofeedback:
scalp potential differences become audible feedback without sampling or DSP in
the feedback loop. Optimize for a fast, understandable proof of concept using
available parts. Expose limitations and latency/filtering tradeoffs rather than
hiding them behind unnecessary complexity.

## Architecture and invariants

`headgames.kicad_sch` is the sole authoritative schematic. The externally
wired MVP uses a keyed 9 V isolated-battery input and three electrodes (`MEAS`,
`REF`, `BIAS`), an inventory-first LM324N for buffered mid-supply reference, AC-coupled
differential acquisition, bounded adjustable alpha-band gain with approximately
7.9-8.8 Hz high-pass and 10.7-12.4 Hz low-pass corner ranges, and envelope-controlled
audible oscillation, plus an LM386 for speaker output.
Each electrode enters through two independent series current-limiting
resistors: 100 kΩ + 100 kΩ on MEAS and REF, and 1 MΩ + 1 MΩ on BIAS.
These resistors do not relax the isolated-battery-only operating rule.
An LM358N precision peak detector converts filtered alpha amplitude into
`ENV`: U3A compensates the forward drop of D1 inside its feedback loop, R12/C9
hold the raw envelope relative to VREF, and U3B buffers that node before R16
controls U1D. D1 is a common 1N4148 small-signal silicon diode: the active
feedback loop compensates its forward drop, while its low reverse leakage keeps
the envelope release dominated by R12/C9. Do not substitute a leaky power
Schottky, which can materially alter the held envelope.

The analog feedback path must remain entirely analog. Future digital logging
must be electrically safe and outside that path. The LM324N is an
inventory-first compromise whose bipolar input bias, offset, and noise limit
the 10 MΩ acquisition network; preserve a clear replacement boundary for
future electrode-side buffers or an instrumentation amplifier, but do not make
an unavailable amplifier mandatory for the MVP.

`manage.py simulate-eeg` is the repeatable small-signal validation path. It
extracts component values from the authoritative schematic and models a 20 kΩ
electrode source per input. At midpoint trims it predicts about 2,433 V/V at
10 Hz and a broad 3.69-14.71 Hz -3 dB span, with a response maximum near
7.53 Hz. Treat this as evidence that the MVP has useful EEG-scale gain but weak
alpha selectivity, not as a substitute for noise, artifact, tolerance, or
isolated bench validation.

Anything conductively connected to electrodes must use an isolated wired DC
supply with no mains-earth or USB connection while worn. Remove grounded
scopes, non-isolated bench supplies, USB, chargers, powered audio, and other
mains-connected equipment before attaching electrodes. This prototype is not a
medical device.

## Current big tasks

- Bench-validate power, VREF, carrier/audio, differential response, filtering,
  and envelope behavior with no person connected.
- Validate isolated-supply physiological pickup: artifacts first, then repeated
  eyes-open/eyes-closed alpha trials.
- Record observed limitations before adding input buffers, an instrumentation
  amplifier, sharper filters, or independent digital logging.

## Working method

Use installed KiCad library symbols and validate the native schematic with
`kicad-cli`. Preserve user layout changes and avoid parallel schematic sources.
Use the root Typer/Rich `manage.py` entrypoint for durable automated checks; do
not add ad-hoc test scripts. Reuse existing modules, keep safety annotations
conspicuous, and commit each verified logical checkpoint with a detailed
message.
