# Nominal ngspice compatibility model

`lmx24_lmx58_nominal.lib` is a project-owned compact model for the nominal
9 V, VREF-centered, 0.5–100 Hz operating frontier. It uses ordinary ngspice
elements and nominal LM324-N/LM358-N datasheet parameters: 100 dB open-loop
gain, 1 MHz GBW, 2 mV input offset, 45 nA mean input bias, 5 nA input-offset
current, 10 MΩ differential input impedance, 4 GΩ common-mode input impedance,
and finite output resistance. Output/current headroom is checked separately by
the frontier so a hard behavioral rail clamp cannot invalidate ngspice's AC
linearization.

This is intentionally not a translation or substitute for TI's comprehensive
PSpice macro-model. It excludes ESD, phase reversal, short-circuit behavior,
temperature extremes, and operation outside the declared nominal frontier.
Its purpose is an independently solved, source-locked ngspice cross-check of
the physical MFB network and acquisition DC balance.
