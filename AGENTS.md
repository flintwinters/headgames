# Project guidance

## Motivation

Headgames seeks the smallest credible direct analog EEG sonification path:
scalp potential differences become audible continuously, without sampling,
DSP, analysis windows, peak hold, or envelope release. Favor low latency and
an understandable proof of concept; measure analog group delay rather than
calling the circuit delay-free. Expose limitations and avoid complexity that
does not improve the physiological experiment.

## Architecture and invariants

`headgames.kicad_sch` is the sole authoritative schematic. The intended worn
path is wet active electrodes, differential acquisition, selected low-delay
alpha weighting, direct bipolar control of the roughly 700 Hz U2D relaxation
oscillator, the gain-20 LM386 stage, and speaker current. The current native
schematic still contains the historical LM358/1N4148 peak detector and is not
yet reconciled; model acceptance therefore remains closed.

MEAS and REF each pass through two independent 100 kΩ resistors before the
electrode-site LM358N buffer. BIAS uses two independent 1 MΩ resistors. The
active assembly boundary has exactly five conductors: buffered MEAS, buffered
REF, BIAS, isolated VCC, and isolated return. The entire worn system remains
battery-isolated. Remove grounded scopes, bench supplies, USB, chargers,
powered audio, and every mains-earth path while worn. This is not a medical
device.

Acquisition ratios remain assembly invariants. Pair-match `R12/R22` (10 MΩ),
`R15/R21` (reserved measured 474 kΩ pair), `C12/C14` (100 nF), and `C11/C15`
(1.5 nF equivalent). Each 1.5 nF network is one 1 nF C0G in parallel with two
series 1 nF C0G parts; show every physical part. A stocked 1 nF + 470 pF pair
is acceptable; a lone 1 nF is not equivalent.

Current model evidence is:

| Model | Result |
|---|---|
| Broad ALPHA candidate | 34.13 ms worst 8–12 Hz group delay; alpha/artifact ratio 0.1726; selected normalized Pareto knee |
| One stocked MFB reference | 82.41 ms; ratio 1.2699 |
| Two-MFB historical reference | 134.51 ms; ratio 9.1975; report-only |
| Stateful U2D/LM386 behavior | About 682 Hz carrier; finite swing, duty, sidebands, harmonics, current, clipping, and latching are reported |
| TI cable transient | 100 Ω fails the prior 250 pF overshoot gate; the smallest passing stocked value is 8.2 kΩ with 0.0% measured overshoot and 8.4 µs settling |

The LM386 model is bounded by its official gain-20, 50 kΩ input, and 300 kHz
bandwidth characteristics. Its nonlinear output claims remain bench-gated.
Wet electrodes gate; dry-electrode reports are informational. The detector
and two-MFB implementations are historical evidence, not hardware candidates.

## Current big tasks

- Establish loop-return phase margin for the selected 8.2 kΩ cable isolation,
  then reconcile both active buffers and the five-wire boundary in KiCad.
- Extend the frontier runner so all requested seeded physical builds and phase
  combinations genuinely exercise each independent stocked part through the
  complete transient speaker-current model.
- Implement the selected broad ALPHA-to-U2D control path in KiCad: synthesize
  `R6`, remove the LM358 detector, diode, hold network, `HOLD`, `DRV`, and
  `ENV`, and prove exact native-netlist equivalence through the speaker.
- Extend characterization across VREF, supply, noise, slew, swing, clipping,
  onset/offset, and carrier-edge response. The attempted TI VREF transient
  fixture was removed because the vendor macro-model could not establish its
  operating point.
- Bench-validate the electrode-input-to-speaker-current path with an isolated
  phantom and nobody connected before any physiological test.

## Working method

Use stock KiCad symbols and validate the native schematic with `kicad-cli`.
Never edit it while its lock exists. Preserve user layout and UI artifacts and
avoid parallel schematic sources. Put durable checks behind the Typer/Rich
root `manage.py`; do not create ad-hoc tests. Reuse existing modules, keep
safety annotations conspicuous, and commit each verified logical checkpoint
with a detailed message.
