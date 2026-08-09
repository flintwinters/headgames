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
| Two-biquad filter | 9.798 Hz center, Q 1.576 per section, ideal 8–12 Hz span | Pass: 565% passive, 1,046% active |
| Filter coefficient corners | Independent ±2% center and ±5% Q | Pass: ≥502% passive, ≥908% active |

The active-electrode sensitivity model assumes 5 pF input capacitance, 1 MHz
GBW, 100 Ω output resistance, 10 pA bias, 25 nV/√Hz white noise, and unequal
150/250 pF cables. The sharper-filter result uses ideal biquads; physical
passive synthesis, correlated tolerances, filter noise, op-amp limits,
saturation, overload recovery, electrode nonlinearities, and isolated phantom
measurement remain outstanding.

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
