# Headgames

Headgames is an isolated-battery, entirely analog EEG-to-audio proof of
concept. [`headgames.kicad_sch`](headgames.kicad_sch) is the sole authoritative
schematic: three protected electrodes feed matched LM324N acquisition and
alpha filtering, an LM358N precision envelope detector, an analog carrier, and
an LM386 speaker stage.

## Safety

This prototype is not a medical device. While electrodes are attached, power
it only from the keyed isolated 9 V battery input (`J1`: pin 1 positive, pin 3
return, pin 2 unused). Disconnect grounded scopes, bench supplies, USB,
chargers, powered audio, and every other mains-earth path.

MEAS and REF each require two independent 100 kΩ series safety resistors; BIAS
requires two independent 1 MΩ resistors. Active-electrode experiments must put
both resistors ahead of each electrode-side buffer and keep the complete worn
system isolated.

## Verification

Use the single durable entrypoint:

```sh
python3 manage.py test
python3 manage.py compare-physical-frontier
python3 manage.py accept
python3 manage.py simulate-eeg
python3 manage.py simulate-artifacts
python3 manage.py simulate-active-electrodes
python3 manage.py simulate-sharper-filter
python3 manage.py simulate-filter-network
python3 manage.py simulate-filter-stress --tier build --samples 2000 --seed 1212498244
```

Current circuit acceptance status: **CLOSED because the physical frontier and
native KiCad schematic do not yet implement the same circuit.** The active
electrode buffers and two MFB sections exist only in the model; carrier/audio
exists only downstream in KiCad. Physical bench validation also remains
required after reconciliation.

The neurofeedback acceptance criterion is at least 25%. Simulation and report
commands may exit successfully after reproducing a documented failing result;
only `python3 manage.py accept` expresses candidate hardware acceptance.

| Command | Exit zero means |
|---|---|
| `python3 manage.py test` | Documented regression expectations were reproduced; no acceptance claim |
| `python3 manage.py simulate-*` | The requested model/report completed, even if its scientific verdict is failure |
| `python3 manage.py accept` | Every declared neurofeedback and hardware gate passed |

The simulations extract schematic values and verify the simulated acquisition
topology against the native netlist. They use a tight nominal operating band
and declared signal fixtures, not guaranteed physiology. The survival fixture combines 20/100 kΩ
electrodes, 1 mV differential motion at 2 Hz, 100 µV differential muscle-like
activity at 30 Hz, 100 mV common-mode mains at 60 Hz, and optionally 50 µV
differential alpha at 10 Hz.

| Model | Principal result | Verdict |
|---|---|---|
| Existing path | 2,433 V/V at 10 Hz; peak 7.53 Hz; −3 dB span 3.69–14.71 Hz | Adequate gain, weak selectivity |
| Artifact fixture | `ENV` changes 3.8% with alpha | Fail |
| Active electrodes | Mains falls 0.686→0.004 V, but motion reaches 1.000 V and `ENV` changes 2.8% | Fail; buffers solve cable imbalance, not motion |
| Ideal two-biquad target | 9.798 Hz center, Q 1.576 per section; 565% passive and 1,046% active | Non-gating synthesis target only |
| Ideal coefficient perturbations | Independent ±2% center and ±5% Q; ≥502% passive and ≥908% active | Non-gating target only |
| Active-electrode physical MFB Python tier | Nominal + 2,000 seeded builds with independent ±1% R/±5% C movement: ≥538.5% `ENV` change; 1.073 V minimum node margin; 1.381 mA peak current | **Pass in Python tier; not a yield estimate** |
| Nominal ngspice compatibility model | 43.86 mV acquisition DC error; Python/SPICE MFB agreement within 0.0000 dB and 0.0002° | **Pass for declared nominal scope** |
| Comprehensive TI PSpice model | TI Rev. C LMx58/LM2904 model remains incompatible with ngspice 44.2 `IF()`/switch syntax | Retained for provenance; outside the compatibility-model gate |

The active-electrode sensitivity model assumes 5 pF input capacitance, 1 MHz
GBW, 100 Ω output resistance, 10 pA bias, 25 nV/√Hz white noise, and unequal
150/250 pF cables. The sharper-filter figures are ideal transfer-function
targets, not a hardware pass. Those baseline figures use ideal biquads and the
explicitly named ideal envelope oracle; they are not physical-detector evidence.

The physical frontier uses the declared electrode-site unity buffers followed
by the existing finite-A0/GBW LM324 acquisition path and two identical
VREF-biased MFB stages, each using
255 kΩ, 64.9 kΩ, 510 kΩ, and two 100 nF parts with one additional LM358N
package. Its Python model exposes internal nodes and branch currents, applies
finite LM324 A0/GBW and nominal bias/offset headroom, and steps a finite-GBW,
slew- and rail-limited LM358 detector with explicit 1N4148 forward current,
leakage, capacitance, hold R/C, and recovery state. The operating band is
8.8–9.2 V with independent ±1% resistor and ±5% capacitor movement, matching
ordinary 1%-resistor/5%-capacitor assembled-part tolerances rather than an
unusually tight hand-selected build. Acquisition DC error
uses typical 5 nA input-offset current through the matched 10 MΩ paths and 2 mV
offset at unity DC noise gain; common input bias current is not incorrectly
treated as wholly unmatched.
The source-locked nominal compatibility model independently cross-checks MFB AC
and matched-10 MΩ acquisition DC behavior in ngspice. It does not reproduce the
comprehensive TI model, establish probabilistic yield, or replace bench
validation. VREF remains ideal and the behavioral amplifier is not
transistor-level.

## Matched acquisition parts

Common-mode rejection requires matched impedance ratios. Pair-match on the
same meter range:

- `R12/R22`: 10 MΩ; `R15/R21`: reserved measured 474 kΩ pair.
- `C12/C14`: 100 nF; `C11/C15`: 1.5 nF equivalent.

Inventory lacks 1.5 nF parts. At each feedback location use one 1 nF C0G in
parallel with two series 1 nF C0G parts, then match the two complete networks.
Show all six capacitors in the schematic. A 1 nF + 470 pF parallel pair is an
acceptable 1.47 nF alternative; a lone 1 nF is not—it moves the nominal corner
from about 10.6 to 15.9 Hz.

The schematic uses only stock KiCad libraries listed in `sym-lib-table`.
