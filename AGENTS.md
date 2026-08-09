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
schematic implements the same selected path, including the two LM358N active
buffers, independent 8.2 kΩ cable isolation, the five-wire assembly boundary,
explicit continuous MEAS/REF wiring through those buffers, and direct
ALPHA-to-R6 control. Core signal-path continuity must not be hidden behind
same-sheet label jumps. Model acceptance remains closed because the
nominal noisy end-to-end experiment fails its alpha-modulation gate.

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

The previous sonification-frontier outputs are invalid and must not guide the
circuit. They did not execute their advertised build count or phase sweep,
used pre-oscillator proxies instead of the declared speaker-current endpoint,
and did not propagate the complete simultaneous stimulus through the physical
path. Do not cite the reported Pareto knee, group delays, modulation ratios,
sidebands, or candidate selection as design evidence. The TI cable transient
is a limited block-level result, not end-to-end sonification validation.

Current limited block-level evidence is:

| Model | Result |
|---|---|
| Stateful oscillator prototype | Produces a nominal carrier near 682 Hz; not validated with the complete electrode-to-speaker stimulus or physical spreads |
| LM386 behavioral prototype | Implements nominal gain, input resistance, bandwidth, clipping, and load; not yet sufficient for output-power or nonlinear claims |
| TI cable transient | 100 Ω fails the prior 250 pF overshoot gate; the smallest passing stocked value is 8.2 kΩ with 0.0% measured overshoot and 8.4 µs settling |
| End-to-end nominal wet/alpha run | Executes all 16 requested phases at the speaker-current endpoint, but fails: worst alpha/carrier modulation is 0.24% versus the 1% gate |

The LM386 model is bounded by its official gain-20, 50 kΩ input, and 300 kHz
bandwidth characteristics. Its nonlinear output claims remain bench-gated.
Wet electrodes gate; dry-electrode reports are informational. The detector
and two-MFB implementations are historical evidence, not hardware candidates.

## Current big tasks

- Establish loop-return phase margin for the selected 8.2 kΩ cable isolation.
- Finish hardening the genuine end-to-end electrode-to-speaker-current experiment.
  Every requested seeded build and phase combination must exercise every
  relevant independent stocked part and the complete simultaneous stimulus.
- Select the weighting topology and synthesize `R6` only after that valid
  experiment produces a reproducible frontier. Do not implement a candidate
  in KiCad based on the invalid nominal knee.
- Extend characterization across VREF, supply, noise, slew, swing, clipping,
  onset/offset, and carrier-edge response. The attempted TI VREF transient
  fixture was removed because the vendor macro-model could not establish its
  operating point.
- Bench-validate the electrode-input-to-speaker-current path with an isolated
  phantom and nobody connected before any physiological test.

## Working method

### Simulation integrity

A simulation exists to produce evidence that can support a stated decision.
If it cannot do that, do not present it as validation. A plausible-looking
number from an incomplete experiment is actively harmful because it creates
false confidence.

- Every command must execute every option and contract it advertises. Never
  accept or print a sample count, phase count, tolerance range, stimulus, or
  gate that the implementation did not actually exercise.
- Measure acceptance at the declared physical endpoint. Do not silently
  replace speaker-current modulation with a filter-node proxy, complete-path
  stability with a block-level transient, or physical behavior with an ideal
  transfer function.
- Propagate the complete declared stimulus through the complete declared path,
  including simultaneous signals, state, nonlinearities, loading, clipping,
  recovery, supply/VREF conditions, and independently moved physical parts
  wherever those effects are in scope.
- Make omissions, idealizations, model boundaries, and bench-gated claims
  explicit in both code and output. Analytical and block-level models may be
  useful, but they must be labeled accurately and cannot claim end-to-end
  circuit validation.
- Fail closed when a model, dependency, cross-check, or requested coverage is
  missing. Do not emit a selection, acceptance verdict, Pareto frontier, or
  other decision-shaped output from incomplete evidence.
- Durable tests must prove requested case counts, deterministic replay,
  independent tolerance movement, phase coverage, stimulus coverage, endpoint
  measurement, rejection gates, and deliberate failure of incomplete runs.
- Apply identical experiments and gates to every compared candidate. Perform
  no ranking or mathematical knee selection until every candidate has
  completed the same valid experiment.
- Treat any discovered placeholder, ignored option, proxy mislabeled as an
  endpoint metric, or unexercised acceptance gate as a correctness defect.
  Invalidate its prior results immediately and remove or disable the interface
  until it becomes real.

Use stock KiCad symbols and validate the native schematic with `kicad-cli`.
Never edit it while its lock exists. Preserve user layout and UI artifacts and
avoid parallel schematic sources. Put durable checks behind the Typer/Rich
root `manage.py`; do not create ad-hoc tests. Reuse existing modules, keep
safety annotations conspicuous, and commit each verified logical checkpoint
with a detailed message.
