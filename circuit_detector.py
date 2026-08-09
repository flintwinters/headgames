"""Ideal and bounded physical envelope-detector simulations.

This module is an implementation detail of :mod:`circuit_sim`; the public
names remain re-exported there for compatibility with existing callers.
"""

from __future__ import annotations

from dataclasses import dataclass
import cmath
import math


@dataclass(frozen=True)
class EnvelopeResult:
    """Steady-window statistics from the ideal precision peak detector."""

    minimum_v: float
    mean_v: float
    maximum_v: float


@dataclass(frozen=True)
class AmplifierLimits:
    """Explicit conservative single-supply op-amp limits for transient checks."""

    name: str
    dc_open_loop_gain: float
    gain_bandwidth_hz: float
    input_offset_v: float
    input_bias_a: float
    slew_v_s: float
    output_low_v: float
    output_high_headroom_v: float
    output_current_a: float
    common_mode_low_v: float
    common_mode_high_headroom_v: float
    overload_recovery_s: float


@dataclass(frozen=True)
class DiodeModel:
    """Bounded large-signal 1N4148 model used by the precision detector."""

    name: str = "1N4148"
    forward_drop_v: float = 0.55
    series_resistance_ohm: float = 2.0
    reverse_leakage_a: float = 25e-9
    junction_capacitance_f: float = 4e-12


@dataclass(frozen=True)
class PhysicalEnvelopeResult:
    """Detector statistics plus the physical limits exercised by the run."""

    envelope: EnvelopeResult
    minimum_output_margin_v: float
    minimum_common_mode_margin_v: float
    peak_diode_current_a: float
    peak_output_current_a: float
    clipped_samples: int
    common_mode_violations: int


def simulate_ideal_peak_detector(
    output_tones: tuple[tuple[float, complex], ...],
    release_seconds: float,
    duration_seconds: float = 6.0,
    sample_rate_hz: float = 2_000.0,
    measurement_seconds: float = 2.0,
) -> EnvelopeResult:
    """Non-gating mathematical zero-drop envelope oracle."""
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


def simulate_precision_peak_detector(
    output_tones: tuple[tuple[float, complex], ...],
    release_resistance_ohm: float,
    hold_capacitance_f: float,
    supply_v: float,
    vref_v: float,
    amplifier: AmplifierLimits,
    diode: DiodeModel,
    duration_seconds: float = 6.0,
    sample_rate_hz: float = 10_000.0,
    measurement_seconds: float = 2.0,
) -> PhysicalEnvelopeResult:
    """Step the LM358/1N4148 precision detector with finite physical limits.

    The first amplifier drives the diode and held capacitor; the second is a
    finite-GBW, slew-limited follower. Diode forward current, leakage and
    junction capacitance are explicit. Values are relative to VREF externally
    but every common-mode/output check is performed in absolute volts.
    """
    if release_resistance_ohm <= 0 or hold_capacitance_f <= 0:
        raise ValueError("detector R and C must be positive")
    if measurement_seconds > duration_seconds or sample_rate_hz <= 0:
        raise ValueError("invalid physical detector interval")
    dt = 1 / sample_rate_hz
    sample_count = int(duration_seconds * sample_rate_hz)
    measurement_start = sample_count - int(measurement_seconds * sample_rate_hz)
    upper_output = supply_v - amplifier.output_high_headroom_v
    upper_common = supply_v - amplifier.common_mode_high_headroom_v
    effective_capacitance = hold_capacitance_f + diode.junction_capacitance_f
    dominant_tau = 1 / (2 * math.pi * amplifier.gain_bandwidth_hz)
    pole_fraction = 1 - math.exp(-dt / dominant_tau)
    closed_loop = amplifier.dc_open_loop_gain / (amplifier.dc_open_loop_gain + 1)
    drive = vref_v
    held = vref_v
    follower = vref_v
    recovery_remaining = 0.0
    measured: list[float] = []
    minimum_output_margin = math.inf
    minimum_common_margin = math.inf
    peak_diode_current = 0.0
    peak_output_current = 0.0
    clipped_samples = 0
    common_mode_violations = 0

    def slew(current: float, target: float) -> float:
        maximum_step = amplifier.slew_v_s * dt
        return current + max(-maximum_step, min(maximum_step, target - current))

    for index in range(sample_count):
        time = index * dt
        relative_input = sum(
            (phasor * cmath.exp(2j * math.pi * frequency * time)).real
            for frequency, phasor in output_tones
        )
        detector_input = vref_v + relative_input
        common_margin = min(
            detector_input - amplifier.common_mode_low_v,
            upper_common - detector_input,
            held - amplifier.common_mode_low_v,
            upper_common - held,
        )
        minimum_common_margin = min(minimum_common_margin, common_margin)
        if common_margin < 0:
            common_mode_violations += 1

        error_target = detector_input + amplifier.input_offset_v
        conducting = error_target > held
        target_drive = (
            vref_v + (error_target - vref_v) * closed_loop + diode.forward_drop_v
            if conducting else amplifier.output_low_v
        )
        if recovery_remaining > 0:
            recovery_remaining = max(0.0, recovery_remaining - dt)
            target_drive = drive
        drive = slew(drive, drive + pole_fraction * (target_drive - drive))
        unclipped_drive = drive
        drive = min(upper_output, max(amplifier.output_low_v, drive))
        # Low-rail parking while off is intentional; upper-rail contact during
        # a charging excursion is the clipping condition relevant here.
        if unclipped_drive > upper_output:
            clipped_samples += 1
            recovery_remaining = max(recovery_remaining, amplifier.overload_recovery_s)

        forward_current = max(
            0.0, (drive - held - diode.forward_drop_v) / diode.series_resistance_ohm
        )
        forward_current = min(forward_current, amplifier.output_current_a)
        release_current = max(0.0, (held - vref_v) / release_resistance_ohm)
        held += dt * (
            forward_current - release_current - diode.reverse_leakage_a
            - amplifier.input_bias_a
        ) / effective_capacitance
        held = min(upper_output, max(vref_v, held))
        follower_target = held + amplifier.input_offset_v
        follower = slew(
            follower,
            follower + pole_fraction * (follower_target * closed_loop - follower),
        )
        follower = min(upper_output, max(amplifier.output_low_v, follower))
        follower_current = abs((follower - held) / release_resistance_ohm)
        peak_diode_current = max(peak_diode_current, forward_current)
        peak_output_current = max(peak_output_current, forward_current, follower_current)
        minimum_output_margin = min(
            minimum_output_margin,
            upper_output - drive,
            follower - amplifier.output_low_v,
            upper_output - follower,
        )
        if index >= measurement_start:
            measured.append(max(0.0, follower - vref_v))

    return PhysicalEnvelopeResult(
        EnvelopeResult(min(measured), sum(measured) / len(measured), max(measured)),
        minimum_output_margin,
        minimum_common_margin,
        peak_diode_current,
        peak_output_current,
        clipped_samples,
        common_mode_violations,
    )
