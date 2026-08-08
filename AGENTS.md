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
differential acquisition, 8.7-12.4 Hz alpha-band gain, and envelope-controlled
audible oscillation, plus an LM386 for speaker output.
A passive detector converts filtered alpha amplitude into `ENV`; `ENV` is not
shorted to `VREF`—R12/C9 return it to VREF, D1 drives it, and it controls U1D
through R16. D1 is specifically a 1N5711 zero-bias Schottky detector; do not
silently substitute a generic power Schottky with different low-level behavior.

The analog feedback path must remain entirely analog. Future digital logging
must be electrically safe and outside that path. The LM324N is an
inventory-first compromise whose bipolar input bias, offset, and noise limit
the 10 MΩ acquisition network; preserve a clear replacement boundary for
future electrode-side buffers or an instrumentation amplifier, but do not make
an unavailable amplifier mandatory for the MVP.

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
