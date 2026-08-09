# Project guidance

## Motivation

Headgames seeks the smallest credible direct analog EEG neurofeedback path:
scalp potential differences become audible without sampling or DSP. Favor a
fast, understandable proof of concept, expose limitations, and avoid complexity
that does not improve the core physiological experiment.

## Architecture and invariants

`headgames.kicad_sch` is the sole authoritative schematic. The isolated 9 V
MVP contains three electrodes (`MEAS`, `REF`, `BIAS`), matched AC-coupled
LM324N acquisition, adjustable alpha gain/filtering, an LM358N precision peak
detector with 1N4148 and 0.22 s release, analog carrier control, and an LM386
speaker stage. The analog feedback path must remain entirely analog; any future
logging stays electrically safe and outside it.

MEAS and REF each enter through two independent 100 kΩ series resistors; BIAS
uses two independent 1 MΩ resistors. These do not relax the isolated-battery
rule. While worn, remove grounded scopes, bench supplies, USB, chargers,
powered audio, and every mains-earth path. This is not a medical device.

Acquisition ratios are assembly invariants. Pair-match `R12/R22` (10 MΩ),
`R15/R21` (reserved measured 474 kΩ pair), `C12/C14` (100 nF), and `C11/C15`
(1.5 nF equivalent). Build each 1.5 nF feedback network from one 1 nF C0G in
parallel with two series 1 nF C0G parts; match the totals and show all physical
parts in the schematic. A 1 nF + 470 pF pair is acceptable when available; a
lone 1 nF is not equivalent.

The root `manage.py` is the sole verification entrypoint. Current architectural
evidence is:

| Model | Result |
|---|---|
| Existing path | 2,433 V/V at 10 Hz; broad 3.69–14.71 Hz −3 dB span |
| Artifact survival | Alpha changes mean `ENV` 3.8%, below the 25% target |
| Active electrodes | Mains improves 0.686→0.004 V, but differential motion remains; `ENV` changes 2.8% |
| Ideal two-biquad target | 9.798 Hz center, Q 1.576/section; ideal `ENV` changes 565% passive and 1,046% active; non-gating |
| Ideal coefficient perturbations | ±2% center, ±5% Q remain ≥502% passive and ≥908% active; non-gating |
| Active-electrode physical MFB Python tier | Nominal + 2,000 seeded builds with independent ±1% R/±5% C movement retain ≥538.5% `ENV` change, 1.073 V minimum node margin, and 1.381 mA peak current; pass, but not a yield estimate |
| Nominal ngspice compatibility model | 43.86 mV acquisition DC error; Python/SPICE MFB agreement within 0.0000 dB and 0.0002°; pass for declared nominal scope |
| Comprehensive TI PSpice model | Retained unchanged for provenance; ngspice 44.2 still rejects its `IF()`/switch syntax |

Thus active electrodes plus sharper selectivity are the current testing
frontier, and the declared nominal simulation gate passes. The
proposed two-stage MFB network has strong modeled selectivity. Its build
frontier uses ordinary ±1% resistor and ±5% capacitor tolerances while keeping
supply and environmental conditions nominal. Acquisition DC error
uses typical input-offset current through the matched 10 MΩ paths and unity DC
noise gain because the 474 kΩ input arms are AC-coupled. The model now includes
active-electrode input/cable behavior, finite acquisition loop gain, and a stateful
LM358/1N4148 detector; VREF, detailed acquisition transients, physical-detector
cross-validation, and bench validation remain incomplete. The project-owned,
source-locked ngspice model independently confirms nominal acquisition DC
balance and MFB AC response; it deliberately does not reproduce the full TI
PSpice macro-model. Active
electrodes address cable/source imbalance but not motion; if pursued, both
safety resistors must precede every electrode-site buffer and the worn supply
must remain isolated. The LM324N remains an inventory-first compromise with a
clear future boundary for buffers or an instrumentation amplifier.

## Current big tasks

- Validate the nominal LM324 acquisition DC balance, MFB response, and detector
  behavior on the bench before considering a schematic edit.
- Extend VREF, temperature, trim spreads, deterministic phases, and detailed
  acquisition transient recovery, then independently validate the model.
- Implement the matched 474 kΩ pair and matched 1.5 nF networks after inventory
  confirmation.
- Reproduce the survival fixture with an isolated physical EEG phantom.
- Bench-validate power, VREF, carrier/audio, acquisition, filtering, and envelope
  with nobody connected; only then test isolated physiological pickup.
- Record observed limitations before adding buffers, instrumentation gain,
  independent logging, or other complexity.

## Working method

Use stock KiCad symbols and validate the native schematic with `kicad-cli`.
Preserve user layout changes and avoid parallel schematic sources. Put all
durable checks behind the Typer/Rich `manage.py`; do not create ad-hoc tests.
Reuse existing modules, keep safety annotations conspicuous, and commit every
verified logical checkpoint with a detailed message.
