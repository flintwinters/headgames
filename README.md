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
python3 manage.py simulate-eeg
python3 manage.py simulate-artifacts
python3 manage.py simulate-active-electrodes
python3 manage.py simulate-sharper-filter
python3 manage.py simulate-filter-network
python3 manage.py simulate-filter-stress --tier build --samples 20000 --seed 1212498244
python3 manage.py simulate-filter-stress --tier abuse --samples 20000 --seed 1212498244
```

The simulations extract schematic values and use declared engineering stress
fixtures, not guaranteed physiology. The survival fixture combines 20/100 kΩ
electrodes, 1 mV differential motion at 2 Hz, 100 µV differential muscle-like
activity at 30 Hz, 100 mV common-mode mains at 60 Hz, and optionally 50 µV
differential alpha at 10 Hz. Passing requires alpha to change mean `ENV` by at
least 25%.

| Model | Principal result | Verdict |
|---|---|---|
| Existing path | 2,433 V/V at 10 Hz; peak 7.53 Hz; −3 dB span 3.69–14.71 Hz | Adequate gain, weak selectivity |
| Artifact fixture | `ENV` changes 3.8% with alpha | Fail |
| Active electrodes | Mains falls 0.686→0.004 V, but motion reaches 1.000 V and `ENV` changes 2.8% | Fail; buffers solve cable imbalance, not motion |
| Ideal two-biquad target | 9.798 Hz center, Q 1.576 per section; 565% passive and 1,046% active | Non-gating synthesis target only |
| Ideal coefficient perturbations | Independent ±2% center and ±5% Q; ≥502% passive and ≥908% active | Non-gating target only |
| Physical MFB Python tier | 1,024 corners + 20,000 seeded builds: ≥523.8% passive `ENV` change; ≥1.669 V modeled node margin; 1.12 µV RMS noise; 0.913 s recovery bound | Partial result only |
| TI-model SPICE cross-check | TI Rev. C LMx58/LM2904 model fails in ngspice 44.2 on PSpice `IF()` and switch syntax | **Fail; hardware gate remains closed** |

The active-electrode sensitivity model assumes 5 pF input capacitance, 1 MHz
GBW, 100 Ω output resistance, 10 pA bias, 25 nV/√Hz white noise, and unequal
150/250 pF cables. The sharper-filter figures are ideal transfer-function
targets, not a hardware pass. The model uses ideal biquads; physical
passive synthesis, correlated tolerances, filter noise, op-amp limits,
saturation, overload recovery, electrode nonlinearities, and isolated phantom
measurement remain outstanding.

The physical candidate is two identical VREF-biased MFB stages, each using
255 kΩ, 64.9 kΩ, 510 kΩ, and two 100 nF parts with one additional LM358N
package. Its Python model exposes internal nodes and branch currents and checks
the passive-electrode fixture. The figures above do not include a successful
independent simulator cross-check, do not establish probabilistic yield, and do
not authorize a schematic edit. The non-gating abuse tier first fails at the
declared 100 mV differential overload because the existing 2,433 V/V
acquisition necessarily saturates.

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
