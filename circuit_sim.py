"""Small-signal circuit simulation for the analog EEG acquisition path.

The model is intentionally expressed in impedances matching the schematic's
two active stages.  It is not a second schematic: component values are always
supplied by ``manage.py`` after extraction from the native KiCad schematic.
"""

from __future__ import annotations

from dataclasses import dataclass
import cmath
import math


@dataclass(frozen=True)
class EegPathComponents:
    """Values needed to solve the differential and alpha-filter stages."""

    electrode_resistance: float
    safety_resistance: float
    input_resistance: float
    input_capacitance: float
    diff_feedback_resistance: float
    diff_feedback_capacitance: float
    alpha_input_resistance: float
    alpha_input_capacitance: float
    alpha_feedback_resistance: float
    alpha_feedback_capacitance: float


@dataclass(frozen=True)
class AcResult:
    """One frequency-domain solution, relative to differential electrode input."""

    frequency_hz: float
    diff_gain: complex
    alpha_gain: complex

    @property
    def total_gain(self) -> complex:
        return self.diff_gain * self.alpha_gain


@dataclass(frozen=True)
class SignalTone:
    """One sinusoidal electrode stimulus represented by peak phasors."""

    frequency_hz: float
    meas_peak_v: complex
    ref_peak_v: complex


@dataclass(frozen=True)
class EnvelopeResult:
    """Steady-window statistics from the ideal precision peak detector."""

    minimum_v: float
    mean_v: float
    maximum_v: float


def parallel(left: complex, right: complex) -> complex:
    """Return the impedance of two parallel branches."""
    return left * right / (left + right)


def capacitor_impedance(capacitance_f: float, frequency_hz: float) -> complex:
    """Return a capacitor's complex impedance at a positive frequency."""
    if frequency_hz <= 0:
        raise ValueError("AC simulation frequency must be positive")
    return 1 / (2j * math.pi * frequency_hz * capacitance_f)


def simulate_ac(parts: EegPathComponents, frequency_hz: float) -> AcResult:
    """Solve both ideal-op-amp stages at one frequency.

    The electrode is a Thevenin source with series impedance.  Symmetry of the
    matched differential amplifier reduces its exact differential-mode nodal
    solution to Zfeedback/Zinput.  The following alpha stage is an inverting
    amplifier with series RC input and parallel RC feedback.
    """
    diff_input = (
        parts.electrode_resistance
        + parts.safety_resistance
        + parts.input_resistance
        + capacitor_impedance(parts.input_capacitance, frequency_hz)
    )
    diff_feedback = parallel(
        complex(parts.diff_feedback_resistance),
        capacitor_impedance(parts.diff_feedback_capacitance, frequency_hz),
    )
    alpha_input = (
        parts.alpha_input_resistance
        + capacitor_impedance(parts.alpha_input_capacitance, frequency_hz)
    )
    alpha_feedback = parallel(
        complex(parts.alpha_feedback_resistance),
        capacitor_impedance(parts.alpha_feedback_capacitance, frequency_hz),
    )
    return AcResult(
        frequency_hz=frequency_hz,
        diff_gain=diff_feedback / diff_input,
        alpha_gain=-alpha_feedback / alpha_input,
    )


def simulate_electrode_inputs(
    parts: EegPathComponents,
    frequency_hz: float,
    meas_peak_v: complex,
    ref_peak_v: complex,
    meas_electrode_resistance: float,
    ref_electrode_resistance: float,
) -> complex:
    """Solve ALPHA for arbitrary electrode phasors and source imbalance.

    Values are relative to VREF.  This is the full ideal-op-amp nodal solution:
    the non-inverting node is first solved as an impedance divider, then the
    inverting-node KCL determines DIFF_OUT.  Unlike ``simulate_ac``, this form
    preserves common-mode-to-differential conversion from unequal electrodes.
    """
    coupling = capacitor_impedance(parts.input_capacitance, frequency_hz)
    meas_input = (
        meas_electrode_resistance
        + parts.safety_resistance
        + parts.input_resistance
        + coupling
    )
    ref_input = (
        ref_electrode_resistance
        + parts.safety_resistance
        + parts.input_resistance
        + coupling
    )
    feedback = parallel(
        complex(parts.diff_feedback_resistance),
        capacitor_impedance(parts.diff_feedback_capacitance, frequency_hz),
    )
    plus_node = meas_peak_v * feedback / (meas_input + feedback)
    diff_out = plus_node + feedback * (plus_node - ref_peak_v) / ref_input
    alpha_input = (
        parts.alpha_input_resistance
        + capacitor_impedance(parts.alpha_input_capacitance, frequency_hz)
    )
    alpha_feedback = parallel(
        complex(parts.alpha_feedback_resistance),
        capacitor_impedance(parts.alpha_feedback_capacitance, frequency_hz),
    )
    return diff_out * (-alpha_feedback / alpha_input)


def simulate_peak_detector(
    output_tones: tuple[tuple[float, complex], ...],
    release_seconds: float,
    duration_seconds: float = 6.0,
    sample_rate_hz: float = 2_000.0,
    measurement_seconds: float = 2.0,
) -> EnvelopeResult:
    """Simulate an ideal zero-drop peak detector with exponential release."""
    if release_seconds <= 0 or measurement_seconds > duration_seconds:
        raise ValueError("invalid envelope simulation interval")
    release_factor = math.exp(-1 / (sample_rate_hz * release_seconds))
    sample_count = int(duration_seconds * sample_rate_hz)
    measurement_start = sample_count - int(measurement_seconds * sample_rate_hz)
    held = 0.0
    measured: list[float] = []
    for index in range(sample_count):
        time = index / sample_rate_hz
        signal = sum(
            (phasor * cmath.exp(2j * math.pi * frequency * time)).real
            for frequency, phasor in output_tones
        )
        held = max(signal, held * release_factor)
        if index >= measurement_start:
            measured.append(held)
    return EnvelopeResult(min(measured), sum(measured) / len(measured), max(measured))


def logarithmic_sweep(
    parts: EegPathComponents,
    start_hz: float = 0.5,
    stop_hz: float = 100.0,
    points: int = 4001,
) -> tuple[AcResult, ...]:
    """Return an inclusive logarithmic AC sweep."""
    if start_hz <= 0 or stop_hz <= start_hz or points < 2:
        raise ValueError("invalid AC sweep bounds")
    ratio = stop_hz / start_hz
    return tuple(
        simulate_ac(parts, start_hz * ratio ** (index / (points - 1)))
        for index in range(points)
    )


def magnitude_db(value: complex) -> float:
    """Return voltage gain in decibels."""
    return 20 * math.log10(abs(value))


def phase_degrees(value: complex) -> float:
    """Return principal phase in degrees."""
    return math.degrees(cmath.phase(value))
