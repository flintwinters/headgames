# Headgames

The current verified build artifact is [BUILD_NOW.svg](BUILD_NOW.svg): a
battery-powered LM324 virtual-ground and carrier oscillator feeding an LM386
speaker amplifier. It deliberately contains no electrode connection. Build and
verify this checkpoint before adding the EEG acquisition path.

An immediately viewable PDF is generated as `build/BUILD_NOW.pdf` with:

```sh
inkscape BUILD_NOW.svg --export-type=pdf --export-filename=build/BUILD_NOW.pdf
```

The previous hand-authored legacy KiCad sheet was removed because KiCad's
render/ERC showed that its wires did not land reliably on symbol pins. It must
not be used for construction.
