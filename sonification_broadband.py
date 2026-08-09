"""Broadband physical twin-T sonification proposal and endpoint experiment."""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from sonification_core import (
    SonificationBuild,
    SonificationCandidate,
    _cascade,
    _filter,
    _mul,
    _speaker_current,
    electrode_to_weighting,
)

BROADBAND_WANTED_HZ = (1.0, 2.0, 4.0, 6.0, 8.0, 10.0, 13.0, 20.0, 30.0)
BROADBAND_SLOW_HZ = (0.1, 0.2, 0.5)
BROADBAND_REJECTION_HZ = (35.0, 40.0, 50.0, 59.0, 60.0, 61.0, 70.0, 80.0, 100.0)


@dataclass(frozen=True)
class BroadbandParts:
    """Physical proposal values; independent leaves are retained explicitly."""

    gain_input_ohm: float
    gain_feedback_ohm: float
    notch_r1_ohm: float
    notch_r2_ohm: float
    notch_r3a_ohm: float
    notch_r3b_ohm: float
    notch_c1_f: float
    notch_c2_f: float
    notch_c3a_f: float
    notch_c3b_f: float
    notch_q_set_ohm: float
    notch_q_feedback_ohm: float

    @property
    def notch_q(self) -> float:
        """Active twin-T Q from its physical positive-feedback divider."""
        return (1+self.notch_q_feedback_ohm/self.notch_q_set_ohm)/4

    @property
    def notch_r3_ohm(self) -> float:
        return 1/(1/self.notch_r3a_ohm+1/self.notch_r3b_ohm)

    @property
    def notch_c3_f(self) -> float:
        return self.notch_c3a_f+self.notch_c3b_f


def _poly_det3(matrix: tuple[tuple[np.ndarray, ...], ...]) -> np.ndarray:
    """Return the determinant of a 3x3 polynomial matrix."""
    positive = np.polyadd(
        np.polymul(matrix[0][0], np.polymul(matrix[1][1], matrix[2][2])),
        np.polyadd(
            np.polymul(matrix[0][1], np.polymul(matrix[1][2], matrix[2][0])),
            np.polymul(matrix[0][2], np.polymul(matrix[1][0], matrix[2][1])),
        ),
    )
    negative = np.polyadd(
        np.polymul(matrix[0][2], np.polymul(matrix[1][1], matrix[2][0])),
        np.polyadd(
            np.polymul(matrix[0][0], np.polymul(matrix[1][2], matrix[2][1])),
            np.polymul(matrix[0][1], np.polymul(matrix[1][0], matrix[2][2])),
        ),
    )
    return np.polysub(positive, negative)


def twin_t_weighting(parts: BroadbandParts) -> tuple[np.ndarray, np.ndarray]:
    """Solve the physical active twin-T network with every passive leaf."""
    constant = lambda value: np.array([value])
    capacitor = lambda value: np.array([value, 0.0])
    g1, g2, g3 = (constant(1/value) for value in (
        parts.notch_r1_ohm, parts.notch_r2_ohm, parts.notch_r3_ohm))
    y1, y2, y3 = (capacitor(value) for value in (
        parts.notch_c1_f, parts.notch_c2_f, parts.notch_c3_f))
    feedback = (parts.notch_q_feedback_ohm /
                (parts.notch_q_set_ohm+parts.notch_q_feedback_ohm))
    matrix = (
        (np.polyadd(np.polyadd(g1, g2), y3), np.array([0.0]),
         np.polysub(np.negative(g2), feedback*y3)),
        (np.array([0.0]), np.polyadd(np.polyadd(y1, y2), g3),
         np.polysub(np.negative(y2), feedback*g3)),
        (np.negative(g2), np.negative(y2), np.polyadd(g2, y2)),
    )
    rhs = (g1, y1, np.array([0.0]))
    numerator_matrix = tuple(
        tuple(rhs[row] if column == 2 else matrix[row][column]
              for column in range(3))
        for row in range(3)
    )
    return _poly_det3(numerator_matrix), _poly_det3(matrix)


@dataclass(frozen=True)
class BroadbandFrequencyResult:
    """One frequency reported at the speaker-current endpoint or as slow AC."""

    frequency_hz: float
    purpose: str
    transfer_gain: float
    delay_s: float
    speaker_modulation_rms_a: float | None
    modulation_to_carrier: float | None


@dataclass(frozen=True)
class BroadbandBuildResult:
    """Complete identity-bearing result for one physical build and candidate."""

    phase_ids: tuple[int, ...]
    frequencies: tuple[BroadbandFrequencyResult, ...]
    carrier_frequency_hz: float
    duty_cycle: float
    minimum_node_margin_v: float
    peak_lm386_current_a: float
    clipped: bool
    latched: bool
    failures: tuple[str, ...]


def broadband_weighting(parts: BroadbandParts) -> tuple[np.ndarray, np.ndarray]:
    """Return the selected flat U2C gain after the physical active twin-T."""
    gain = -parts.gain_feedback_ohm/parts.gain_input_ohm
    numerator, denominator = twin_t_weighting(parts)
    return gain*numerator, denominator


def electrode_to_broadband(build: SonificationBuild, parts: BroadbandParts
                            ) -> tuple[tuple[np.ndarray, np.ndarray],
                                       tuple[np.ndarray, np.ndarray]]:
    """Reuse the physical acquisition path and append the broadband proposal."""
    flat = SonificationCandidate("broadband", 0, 0)
    meas, ref = electrode_to_weighting(build, flat)
    # Remove the legacy constant gain already applied by the compatibility
    # branch before adding the independently specified physical proposal.
    legacy_gain = -build.alpha_feedback_ohm/build.alpha_input_ohm
    undo = (np.array([1/legacy_gain]), np.array([1.0]))
    weighting = broadband_weighting(parts)
    return _cascade(meas, undo, weighting), _cascade(ref, undo, weighting)


def broadband_group_delay(build: SonificationBuild, parts: BroadbandParts,
                          frequency_hz: float) -> float:
    """Return exact differential-path group delay for the proposed topology."""
    meas, ref = electrode_to_broadband(build, parts)
    numerator = np.polyadd(_mul(meas[0], ref[1]), -_mul(ref[0], meas[1]))
    denominator = _mul(meas[1], ref[1])
    s = 2j*math.pi*frequency_hz
    derivative = (
        1j*np.polyval(np.polyder(numerator), s)/np.polyval(numerator, s)
        - 1j*np.polyval(np.polyder(denominator), s)/np.polyval(denominator, s)
    )
    return -float(np.imag(derivative))


def broadband_transfer_gain(build: SonificationBuild, parts: BroadbandParts,
                            frequency_hz: float) -> float:
    """Return differential electrode-to-control magnitude at one frequency."""
    meas, ref = electrode_to_broadband(build, parts)
    s = 2j*math.pi*frequency_hz
    value = np.polyval(meas[0], s)/np.polyval(meas[1], s) - (
        np.polyval(ref[0], s)/np.polyval(ref[1], s))
    return float(abs(value))


def _edge_frequency_amplitude(row: np.ndarray, start: int, stop: int,
                              sample_rate_hz: float, tone_hz: float) -> float:
    """Fit one modulation tone to carrier periods at the physical endpoint."""
    segment = row[start:stop]
    indices = np.flatnonzero(segment[:-1]*segment[1:] < 0)
    if indices.size < 8:
        return 0.0
    crossings = np.asarray([
        (start+index-segment[index]/(segment[index+1]-segment[index]))/sample_rate_hz
        for index in indices
    ])
    periods = crossings[2:]-crossings[:-2]
    times = (crossings[2:]+crossings[:-2])/2
    instantaneous = 1/periods
    instantaneous -= np.mean(instantaneous)
    design = np.column_stack((np.sin(2*math.pi*tone_hz*times),
                              np.cos(2*math.pi*tone_hz*times)))
    return float(np.linalg.norm(np.linalg.lstsq(design, instantaneous, rcond=None)[0]))


def simulate_broadband_build(
    build: SonificationBuild, parts: BroadbandParts, phase_steps: int,
    sample_rate_hz: float = 40_000.0,
) -> BroadbandBuildResult:
    """Exercise every declared tone through the nonlinear speaker endpoint.

    The 0.1--0.5 Hz characterization is deliberately AC-only: resolving it at
    the stateful endpoint would require long onset/offset records and is not an
    acceptance gate. Every wanted and rejection tone is independently driven
    through acquisition, notch, oscillator, LM386, output capacitor, and the
    8-ohm speaker model while 60 Hz common-mode interference is simultaneous.
    """
    if phase_steps < 1:
        raise ValueError("phase_steps must be positive")
    meas_tf, ref_tf = electrode_to_broadband(build, parts)
    duration_s = 2.0
    samples = round(duration_s*sample_rate_hz)
    time = np.arange(samples)/sample_rate_hz
    phases = 2*math.pi*np.arange(phase_steps)/phase_steps
    all_results: list[BroadbandFrequencyResult] = []
    failures: list[str] = []
    carrier_values: list[float] = []
    duty_values: list[float] = []
    minimum_margin = math.inf
    maximum_current = 0.0
    any_clipped = False
    any_latched = False
    for frequency in BROADBAND_WANTED_HZ + BROADBAND_REJECTION_HZ:
        tone = 25e-6*np.sin(2*math.pi*frequency*time[None, :]+phases[:, None])
        mains = 100e-3*np.sin(2*math.pi*60*time)[None, :]
        baseline = np.broadcast_to(mains, tone.shape)
        meas = np.concatenate((baseline, baseline+tone), axis=0)
        ref = np.concatenate((baseline, baseline-tone), axis=0)
        control = _filter(meas_tf, meas, sample_rate_hz)+_filter(ref_tf, ref, sample_rate_hz)
        control += build.vref_v
        currents, edges, states, clipped, peak_current, margin = _speaker_current(
            control, build, sample_rate_hz)
        start, stop = round(0.75*sample_rate_hz), round(1.75*sample_rate_hz)
        tone_modulation = []
        carrier_rms = []
        for phase in range(phase_steps):
            baseline_amplitude = _edge_frequency_amplitude(
                currents[phase], start, stop, sample_rate_hz, frequency)
            combined_amplitude = _edge_frequency_amplitude(
                currents[phase_steps+phase], start, stop, sample_rate_hz, frequency)
            rms = float(np.sqrt(np.mean(currents[phase_steps+phase, start:stop]**2)))
            tone_modulation.append(abs(combined_amplitude-baseline_amplitude)
                                   * rms/max(2*frequency, 1e-30))
            carrier_rms.append(rms)
        transitions = np.count_nonzero(edges[phase_steps:, start:stop], axis=1)
        carrier = transitions/(2*((stop-start)/sample_rate_hz))
        duty = np.mean(states[phase_steps:, start:stop], axis=1)
        mod = min(tone_modulation)
        ratio = min(value/max(rms, 1e-15)
                    for value, rms in zip(tone_modulation, carrier_rms, strict=True))
        purpose = "wanted" if frequency in BROADBAND_WANTED_HZ else "rejection"
        all_results.append(BroadbandFrequencyResult(
            frequency, purpose, broadband_transfer_gain(build, parts, frequency),
            broadband_group_delay(build, parts, frequency), mod, ratio))
        carrier_values.extend(float(value) for value in carrier)
        duty_values.extend(float(value) for value in duty)
        minimum_margin = min(minimum_margin, margin, float(np.min(np.minimum(
            control-build.oscillator_low_v,
            build.supply_v-build.oscillator_high_headroom_v-control))))
        maximum_current = max(maximum_current, peak_current)
        any_clipped |= clipped
        latched = bool(np.any(transitions < 4))
        any_latched |= latched
        if purpose == "wanted" and ratio < 0.001:
            failures.append(f"{frequency:g} Hz: inaudible modulation")
        if not np.all((carrier >= 300) & (carrier <= 1500)):
            failures.append(f"{frequency:g} Hz: carrier frequency")
        if not np.all((duty >= 0.10) & (duty <= 0.90)):
            failures.append(f"{frequency:g} Hz: duty cycle")
        if latched:
            failures.append(f"{frequency:g} Hz: oscillator latching")
    for frequency in BROADBAND_SLOW_HZ:
        all_results.append(BroadbandFrequencyResult(
            frequency, "slow AC only", broadband_transfer_gain(build, parts, frequency),
            broadband_group_delay(build, parts, frequency), None, None))
    wanted = {item.frequency_hz: item for item in all_results if item.purpose == "wanted"}
    rejection = {item.frequency_hz: item for item in all_results if item.purpose == "rejection"}
    reference = wanted[10.0].transfer_gain
    for frequency in (59.0, 61.0):
        rejection_db = 20*math.log10(max(reference, 1e-30)/max(
            rejection[frequency].transfer_gain, 1e-30))
        if rejection_db < 10:
            failures.append(f"{frequency:g} Hz: notch rejection {rejection_db:.1f} dB")
    rejection_60_db = 20*math.log10(max(reference, 1e-30)/max(
        rejection[60.0].transfer_gain, 1e-30))
    if rejection_60_db < 30:
        failures.append(f"60 Hz: notch rejection {rejection_60_db:.1f} dB")
    for frequency, limit in ((4.0, 0.030), (6.0, 0.025), (8.0, 0.025),
                             (10.0, 0.025), (13.0, 0.025), (20.0, 0.025),
                             (30.0, 0.025)):
        if wanted[frequency].delay_s > limit:
            failures.append(f"{frequency:g} Hz: delay {wanted[frequency].delay_s*1e3:.1f} ms")
    if minimum_margin < 0.250:
        failures.append(f"node margin {minimum_margin:.3f} V")
    if maximum_current > 0.5:
        failures.append(f"amplifier current {maximum_current:.3f} A")
    if any_clipped:
        failures.append("behavioral clipping")
    return BroadbandBuildResult(
        tuple(range(phase_steps)), tuple(sorted(all_results, key=lambda item: item.frequency_hz)),
        min(carrier_values), min(duty_values), minimum_margin, maximum_current,
        any_clipped, any_latched, tuple(failures),
    )


