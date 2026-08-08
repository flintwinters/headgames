# Headgames

Open `headgames.sch` in KiCad. It is the battery-powered LM324 virtual-ground
and carrier oscillator feeding an LM386 speaker amplifier. It deliberately
contains no electrode connection. Build and verify this checkpoint before
adding the EEG acquisition path.

An immediately viewable PDF is generated as `build/BUILD_NOW.pdf` with:

```sh
inkscape BUILD_NOW.svg --export-type=pdf --export-filename=build/BUILD_NOW.pdf
```

The schematic references only stock KiCad libraries through `sym-lib-table`:
`Device`, `Switch`, `Amplifier_Operational`, `Amplifier_Audio`, and `power`.
There are no project-drawn substitute symbols.
