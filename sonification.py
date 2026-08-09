"""End-to-end continuous-time analog sonification validation.

Every reported modulation quantity is derived from capacitor-coupled 8-ohm
speaker current.  The complete simultaneous electrode stimulus is propagated
through physical acquisition/filter transfer functions, a stateful finite-
swing relaxation oscillator, and a bounded gain-20 LM386 model.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Callable

import numpy as np
from numba import njit
from scipy import signal


@dataclass(frozen=True)
class SonificationCandidate:
    name: str
    mfb_stages: int
    physical_parts: int


CANDIDATES = (
    SonificationCandidate("broadband", 0, 4),
    SonificationCandidate("alpha", 0, 6),
    SonificationCandidate("mfb1", 1, 11),
    SonificationCandidate("mfb2", 2, 22),
)


@dataclass(frozen=True)
class ChannelParts:
    electrode_series_ohm: float
    electrode_charge_transfer_ohm: float
    electrode_interface_f: float
    buffer_input_f: float
    buffer_bandwidth_hz: float
    buffer_noise_v_rt_hz: float
    safety_a_ohm: float
    safety_b_ohm: float
    input_ohm: float
    input_f: float
    feedback_ohm: float
    feedback_f: float
    cable_isolation_ohm: float
    cable_f: float


@dataclass(frozen=True)
class MfbParts:
    r1_ohm: float
    r2_ohm: float
    r5_ohm: float
    c3_f: float
    c4_f: float


@dataclass(frozen=True)
class SonificationBuild:
    meas: ChannelParts
    ref: ChannelParts
    alpha_input_ohm: float
    alpha_input_f: float
    alpha_feedback_ohm: float
    alpha_feedback_f: float
    mfb: tuple[MfbParts, ...]
    r3_ohm: float
    r4_ohm: float
    r6_ohm: float
    r9_ohm: float
    c10_f: float
    r5_audio_ohm: float
    r8_audio_ohm: float
    c5_audio_f: float
    c6_output_f: float
    r10_zobel_ohm: float
    c7_zobel_f: float
    supply_v: float
    vref_v: float
    oscillator_low_v: float
    oscillator_high_headroom_v: float
    lm386_gain: float
    lm386_input_ohm: float
    lm386_bandwidth_hz: float
    lm386_output_power_w: float
    speaker_ohm: float


@dataclass(frozen=True)
class PhaseMetrics:
    frequency_hz: float
    duty_cycle: float
    speaker_rms_a: float
    alpha_modulation_rms_a: float
    artifact_modulation_rms_a: float
    modulation_ratio: float
    alpha_to_carrier: float
    onset_t10_s: float
    onset_t90_s: float
    offset_t10_s: float
    first_edge_latency_s: float
    minimum_node_margin_v: float
    peak_lm386_current_a: float
    clipped: bool
    latched: bool


@dataclass(frozen=True)
class BuildResult:
    phases_executed: int
    worst: PhaseMetrics
    first_failure: str | None


def _mul(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    return np.polymul(left, right)


def _add(left: tuple[np.ndarray, np.ndarray],
         right: tuple[np.ndarray, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    ln, ld = left
    rn, rd = right
    return np.polyadd(_mul(ln, rd), _mul(rn, ld)), _mul(ld, rd)


def _cascade(*stages: tuple[np.ndarray, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    numerator = np.array([1.0])
    denominator = np.array([1.0])
    for stage_numerator, stage_denominator in stages:
        numerator = _mul(numerator, stage_numerator)
        denominator = _mul(denominator, stage_denominator)
    return numerator, denominator


def _ratio(channel: ChannelParts, divider: bool) -> tuple[np.ndarray, np.ndarray]:
    """Return Zfeedback/Zinput, or Zfeedback/(Zinput+Zfeedback)."""
    source = channel.safety_a_ohm + channel.safety_b_ohm + channel.input_ohm
    numerator = np.array([channel.feedback_ohm * channel.input_f, 0.0])
    denominator = _mul(
        np.array([channel.feedback_ohm * channel.feedback_f, 1.0]),
        np.array([source * channel.input_f, 1.0]),
    )
    if divider:
        denominator = np.polyadd(denominator, numerator)
    return numerator, denominator


def _cable(channel: ChannelParts) -> tuple[np.ndarray, np.ndarray]:
    safety = channel.safety_a_ohm + channel.safety_b_ohm
    series = safety + channel.electrode_series_ohm
    tau = channel.electrode_charge_transfer_ohm*channel.electrode_interface_f
    electrode_input = (
        np.array([tau, 1.0]),
        np.array([
            tau*channel.buffer_input_f*series,
            tau + channel.buffer_input_f*(series+channel.electrode_charge_transfer_ohm),
            1.0,
        ]),
    )
    follower = (np.array([1.0]), np.array([1/(2*math.pi*channel.buffer_bandwidth_hz), 1.0]))
    cable = (np.array([1.0]), np.array([
        channel.cable_isolation_ohm * channel.cable_f, 1.0,
    ]))
    return _cascade(electrode_input, follower, cable)


def electrode_to_weighting(build: SonificationBuild, candidate: SonificationCandidate
                            ) -> tuple[tuple[np.ndarray, np.ndarray],
                                       tuple[np.ndarray, np.ndarray]]:
    """Return independent MEAS/REF transfer functions to oscillator control."""
    plus = _ratio(build.meas, True)
    minus = _ratio(build.ref, False)
    acquisition_noise_gain = 1 + build.meas.feedback_ohm/build.meas.input_ohm
    acquisition_opamp = (np.array([1.0]), np.array([
        acquisition_noise_gain/(2*math.pi*1_000_000.0), 1.0]))
    meas_acquisition = _cascade(_cable(build.meas), plus, acquisition_opamp)
    meas_acquisition = _cascade(meas_acquisition, _add((np.array([1.0]), np.array([1.0])), minus))
    ref_acquisition = _cascade(_cable(build.ref), (-minus[0], minus[1]),
                               acquisition_opamp)

    if candidate.name == "broadband":
        weighting = (np.array([-build.alpha_feedback_ohm/build.alpha_input_ohm]),
                     np.array([1.0]))
    else:
        weighting = (
            np.array([-build.alpha_feedback_ohm*build.alpha_input_f, 0.0]),
            _mul(np.array([build.alpha_input_ohm*build.alpha_input_f, 1.0]),
                 np.array([build.alpha_feedback_ohm*build.alpha_feedback_f, 1.0])),
        )
    alpha_noise_gain = 1 + build.alpha_feedback_ohm/build.alpha_input_ohm
    weighting = _cascade(weighting, (np.array([1.0]), np.array([
        alpha_noise_gain/(2*math.pi*1_000_000.0), 1.0])))
    for parts in build.mfb[:candidate.mfb_stages]:
        weighting = _cascade(weighting, (
            np.array([-parts.c3_f*parts.r2_ohm*parts.r5_ohm, 0.0]),
            np.array([
                parts.c3_f*parts.c4_f*parts.r1_ohm*parts.r2_ohm*parts.r5_ohm,
                (parts.c3_f+parts.c4_f)*parts.r1_ohm*parts.r2_ohm,
                parts.r1_ohm+parts.r2_ohm,
            ]),
        ))
        mfb_noise_gain = 1 + parts.r5_ohm/parts.r1_ohm + parts.r5_ohm/parts.r2_ohm
        weighting = _cascade(weighting, (np.array([1.0]), np.array([
            mfb_noise_gain/(2*math.pi*1_000_000.0), 1.0])))
    return _cascade(meas_acquisition, weighting), _cascade(ref_acquisition, weighting)


def group_delay(build: SonificationBuild, candidate: SonificationCandidate,
                frequency_hz: float) -> float:
    meas, ref = electrode_to_weighting(build, candidate)
    def differential(frequency: float) -> complex:
        s = 2j*math.pi*frequency
        return np.polyval(meas[0], s)/np.polyval(meas[1], s) - (
            np.polyval(ref[0], s)/np.polyval(ref[1], s))
    delta = frequency_hz*1e-4
    return -np.angle(differential(frequency_hz+delta)/differential(frequency_hz-delta))/(4*math.pi*delta)


def closed_form_group_delay(build: SonificationBuild,
                            candidate: SonificationCandidate,
                            frequency_hz: float) -> float:
    """Differentiate the exact rational MEAS-minus-REF transfer analytically."""
    meas, ref = electrode_to_weighting(build, candidate)
    numerator = np.polyadd(_mul(meas[0], ref[1]), -_mul(ref[0], meas[1]))
    denominator = _mul(meas[1], ref[1])
    s = 2j*math.pi*frequency_hz
    logarithmic_derivative = (
        1j*np.polyval(np.polyder(numerator), s)/np.polyval(numerator, s)
        - 1j*np.polyval(np.polyder(denominator), s)/np.polyval(denominator, s)
    )
    return -float(np.imag(logarithmic_derivative))


def _filter(tf: tuple[np.ndarray, np.ndarray], values: np.ndarray,
            sample_rate_hz: float) -> np.ndarray:
    zeros, poles, gain = signal.tf2zpk(tf[0], tf[1])
    digital = signal.bilinear_zpk(zeros, poles, gain, fs=sample_rate_hz)
    sections = signal.zpk2sos(*digital, pairing="nearest")
    return signal.sosfilt(sections, values, axis=-1)


def electrode_stimulus(build: SonificationBuild, phase_steps: int,
                       sample_rate_hz: float, duration_s: float,
                       alpha_on_s: float, alpha_off_s: float, noise_seed: int,
                       noise_enabled: bool,
                       ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return silence, artifacts, and alpha+artifacts as MEAS/REF matrices."""
    time = np.arange(round(duration_s*sample_rate_hz))/sample_rate_hz
    meas = np.empty((phase_steps, time.size))
    ref = np.empty_like(meas)
    gate = ((time >= alpha_on_s) & (time < alpha_off_s)).astype(float)
    for phase in range(phase_steps):
        base = 2*math.pi*phase/phase_steps
        motion = 0.5e-3*np.sin(2*math.pi*2*time + 3*base)
        muscle = 50e-6*np.sin(2*math.pi*30*time + 5*base)
        mains = 100e-3*np.sin(2*math.pi*60*time + 7*base)
        alpha = 25e-6*gate*np.sin(2*math.pi*10*time + base)
        meas[phase] = mains + motion + muscle + alpha
        ref[phase] = mains - motion - muscle - alpha
    artifacts_meas = meas.copy()
    artifacts_ref = ref.copy()
    alpha_component = 25e-6*gate[None, :]*np.sin(
        2*math.pi*10*time[None, :] + 2*math.pi*np.arange(phase_steps)[:, None]/phase_steps)
    artifacts_meas -= alpha_component
    artifacts_ref += alpha_component
    rng = np.random.default_rng(noise_seed)
    boltzmann = 1.380649e-23
    def noise(channel: ChannelParts) -> np.ndarray:
        resistance = (channel.electrode_series_ohm
                      + channel.electrode_charge_transfer_ohm
                      + channel.safety_a_ohm + channel.safety_b_ohm)
        density = math.sqrt(channel.buffer_noise_v_rt_hz**2
                            + 4*boltzmann*300.0*resistance)
        return rng.normal(0.0, density*math.sqrt(sample_rate_hz/2), meas.shape)
    if noise_enabled:
        meas_noise, ref_noise = noise(build.meas), noise(build.ref)
    else:
        meas_noise = np.zeros_like(meas)
        ref_noise = np.zeros_like(ref)
    silence = np.stack((meas_noise, ref_noise))
    artifacts = np.stack((artifacts_meas+meas_noise, artifacts_ref+ref_noise))
    combined = np.stack((meas+meas_noise, ref+ref_noise))
    return silence, artifacts, combined


@njit(cache=True)
def _speaker_current_core(
    control: np.ndarray, sample_rate_hz: float, supply_v: float, vref: float,
    r3_ohm: float, r4_ohm: float, r6_ohm: float, r9_ohm: float, c10_f: float,
    r5_audio_ohm: float, r8_audio_ohm: float, c5_audio_f: float,
    c6_output_f: float, r10_zobel_ohm: float, c7_zobel_f: float,
    oscillator_low_v: float, oscillator_high_headroom_v: float,
    lm386_gain: float, lm386_input_ohm: float, lm386_bandwidth_hz: float,
    lm386_output_power_w: float, speaker_ohm: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, bool, float, float]:
    """JIT kernel for the explicit nonlinear state updates."""
    dt = 1/sample_rate_hz
    lanes, samples = control.shape
    low = oscillator_low_v
    high = supply_v-oscillator_high_headroom_v
    output = np.full(lanes, high)
    timing = np.full(lanes, vref)
    lm_output = np.full(lanes, vref)
    input_coupling_v = np.zeros(lanes)
    coupling_cap_v = np.zeros(lanes)
    zobel_cap_v = np.zeros(lanes)
    speaker = np.empty_like(control)
    edges = np.zeros(control.shape, dtype=np.bool_)
    high_state = np.empty(control.shape, dtype=np.bool_)
    minimum_margin = math.inf
    clipped = False
    lm_alpha = 1-math.exp(-dt*2*math.pi*lm386_bandwidth_hz)
    input_load = 1/(1/r8_audio_ohm + 1/lm386_input_ohm)
    attenuation = input_load/(r5_audio_ohm+input_load)
    input_cap_alpha = 1-math.exp(-dt/((r5_audio_ohm+input_load)*c5_audio_f))
    timing_alpha = 1-math.exp(-dt/(r9_ohm*c10_f))
    output_cap_alpha = 1-math.exp(-dt/(speaker_ohm*c6_output_f))
    zobel_alpha = 1-math.exp(-dt/(r10_zobel_ohm*c7_zobel_f))
    peak_current = 0.0
    previous = output.copy()
    max_peak = math.sqrt(2*lm386_output_power_w*speaker_ohm)
    for index in range(samples):
        for lane in range(lanes):
            threshold = (vref/r3_ohm + output[lane]/r4_ohm
                         + control[lane, index]/r6_ohm) / (
                             1/r3_ohm + 1/r4_ohm + 1/r6_ohm)
            prior_timing = timing[lane]
            timing[lane] += timing_alpha*(output[lane]-timing[lane])
            if output[lane] == high and timing[lane] >= threshold:
                fraction = (threshold-prior_timing)/max(timing[lane]-prior_timing, 1e-30)
                fraction = min(1.0, max(0.0, fraction))
                output[lane] = low
                remainder_alpha = 1-math.exp(-(1-fraction)*dt/(r9_ohm*c10_f))
                timing[lane] = threshold + remainder_alpha*(low-threshold)
            elif output[lane] == low and timing[lane] <= threshold:
                fraction = (prior_timing-threshold)/max(prior_timing-timing[lane], 1e-30)
                fraction = min(1.0, max(0.0, fraction))
                output[lane] = high
                remainder_alpha = 1-math.exp(-(1-fraction)*dt/(r9_ohm*c10_f))
                timing[lane] = threshold + remainder_alpha*(high-threshold)
            edges[lane, index] = output[lane] != previous[lane]
            previous[lane] = output[lane]
            high_state[lane, index] = output[lane] == high
            carrier_ac = output[lane]-(high+low)/2
            input_coupling_v[lane] += input_cap_alpha*(carrier_ac-input_coupling_v[lane])
            input_ac = attenuation*(carrier_ac-input_coupling_v[lane])
            target = vref + lm386_gain*input_ac
            if target < vref-max_peak:
                target = vref-max_peak
                clipped = True
            elif target > vref+max_peak:
                target = vref+max_peak
                clipped = True
            lm_output[lane] += lm_alpha*(target-lm_output[lane])
            coupling_cap_v[lane] += output_cap_alpha*((lm_output[lane]-vref)-coupling_cap_v[lane])
            speaker[lane, index] = ((lm_output[lane]-vref)-coupling_cap_v[lane])/speaker_ohm
            zobel_cap_v[lane] += zobel_alpha*((lm_output[lane]-vref)-zobel_cap_v[lane])
            total_current = speaker[lane, index] + ((lm_output[lane]-vref)-zobel_cap_v[lane])/r10_zobel_ohm
            peak_current = max(peak_current, abs(total_current))
            minimum_margin = min(minimum_margin, threshold-low, high-threshold,
                                 timing[lane]-low, high-timing[lane])
    return speaker, edges, high_state, clipped, peak_current, minimum_margin


def _speaker_current(control: np.ndarray, build: SonificationBuild,
                     sample_rate_hz: float) -> tuple[
                         np.ndarray, np.ndarray, np.ndarray, bool, float, float,
                     ]:
    """Step oscillator, LM386 bandwidth/clipping, output C, speaker and Zobel."""
    return _speaker_current_core(
        control, sample_rate_hz, build.supply_v, build.vref_v,
        build.r3_ohm, build.r4_ohm,
        build.r6_ohm, build.r9_ohm, build.c10_f, build.r5_audio_ohm,
        build.r8_audio_ohm, build.c5_audio_f, build.c6_output_f,
        build.r10_zobel_ohm, build.c7_zobel_f,
        build.oscillator_low_v, build.oscillator_high_headroom_v,
        build.lm386_gain, build.lm386_input_ohm, build.lm386_bandwidth_hz,
        build.lm386_output_power_w, build.speaker_ohm,
    )


def _settling_metrics(delta: np.ndarray, sample_rate_hz: float, on_s: float,
                      off_s: float) -> tuple[float, float, float]:
    # A half alpha cycle gives a phase-independent RMS amplitude estimate.
    bin_seconds = 0.050
    bin_samples = max(1, round(sample_rate_hz*bin_seconds))
    usable = delta.shape[-1]//bin_samples*bin_samples
    envelope = np.sqrt(np.mean(delta[..., :usable].reshape(
        delta.shape[0], -1, bin_samples)**2, axis=-1))
    on_bin, off_bin = round(on_s/bin_seconds), round(off_s/bin_seconds)
    steady = np.mean(envelope[:, max(on_bin, off_bin-2):off_bin], axis=1)
    def crossing(row: np.ndarray, start: int, stop: int, threshold: float, above: bool) -> int:
        found = np.flatnonzero((row[start:stop] >= threshold) if above else (row[start:stop] <= threshold))
        return start + int(found[0]) if found.size else stop
    t10 = max((crossing(row, on_bin, off_bin, 0.1*level, True)-on_bin)*bin_seconds
              for row, level in zip(envelope, steady, strict=True))
    t90 = max((crossing(row, on_bin, off_bin, 0.9*level, True)-on_bin)*bin_seconds
              for row, level in zip(envelope, steady, strict=True))
    offset = max((crossing(row, off_bin, row.size, 0.1*level, False)-off_bin)*bin_seconds
                 for row, level in zip(envelope, steady, strict=True))
    return t10, t90, offset


@njit(cache=True)
def _tone_amplitude(row: np.ndarray, start: int, stop: int,
                    sample_rate_hz: float, frequency_hz: float) -> float:
    mean = 0.0
    for index in range(start, stop):
        mean += row[index]
    mean /= stop-start
    real = 0.0
    imag = 0.0
    for offset, index in enumerate(range(start, stop)):
        angle = 2*math.pi*frequency_hz*offset/sample_rate_hz
        value = row[index]-mean
        real += value*math.cos(angle)
        imag -= value*math.sin(angle)
    return 2*math.sqrt(real*real+imag*imag)/(stop-start)


@njit(cache=True)
def _speaker_spectral_metrics(
    quiet_envelope: np.ndarray, artifact_envelope: np.ndarray,
    combined_envelope: np.ndarray, start: int, stop: int, sample_rate_hz: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    phases = quiet_envelope.shape[0]
    alpha = np.empty(phases)
    artifacts = np.empty(phases)
    carrier_amplitude = np.empty(phases)
    for phase in range(phases):
        total = 0.0
        for index in range(start, stop):
            total += combined_envelope[phase, index]
        carrier_amplitude[phase] = total/(stop-start)
        with_alpha = _tone_amplitude(
            combined_envelope[phase], start, stop, sample_rate_hz, 10.0)
        without_alpha = _tone_amplitude(
            artifact_envelope[phase], start, stop, sample_rate_hz, 10.0)
        alpha[phase] = abs(with_alpha-without_alpha)
        artifact_squared = 0.0
        for frequency in (2.0, 30.0, 60.0):
            with_artifact = _tone_amplitude(
                artifact_envelope[phase], start, stop, sample_rate_hz, frequency)
            without_artifact = _tone_amplitude(
                quiet_envelope[phase], start, stop, sample_rate_hz, frequency)
            artifact_squared += (with_artifact-without_artifact)**2
        artifacts[phase] = math.sqrt(artifact_squared)
    return alpha, artifacts, carrier_amplitude


def _speaker_fft_metrics(
    quiet: np.ndarray, artifact: np.ndarray, combined: np.ndarray,
    start: int, stop: int, sample_rate_hz: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Measure carrier and physical sidebands from windowed speaker current."""
    window = np.hanning(stop-start)
    transform_size = 1 << math.ceil(math.log2(4*(stop-start)))
    frequencies = np.fft.rfftfreq(transform_size, 1/sample_rate_hz)
    scale = 2/max(np.sum(window), 1e-30)
    spectra = [scale*np.abs(np.fft.rfft(values[:, start:stop]*window,
                                        n=transform_size, axis=1))
               for values in (quiet, artifact, combined)]
    carrier_band = np.flatnonzero((frequencies >= 300) & (frequencies <= 1500))
    alpha = np.empty(quiet.shape[0])
    artifacts = np.empty_like(alpha)
    carrier = np.empty_like(alpha)
    for phase in range(quiet.shape[0]):
        centers = [carrier_band[np.argmax(spectrum[phase, carrier_band])]
                   for spectrum in spectra]
        carrier[phase] = spectra[2][phase, centers[2]]
        def sidebands(spectrum: np.ndarray, center: int,
                      offsets: tuple[float, ...]) -> np.ndarray:
            center_hz = frequencies[center]
            return np.array([spectrum[phase, np.argmin(np.abs(
                frequencies-(center_hz+offset)))] for offset in offsets])
        alpha[phase] = float(np.linalg.norm(
            sidebands(spectra[2], centers[2], (-10.0, 10.0))
            - sidebands(spectra[1], centers[1], (-10.0, 10.0))))
        artifacts[phase] = float(np.linalg.norm(
            sidebands(spectra[1], centers[1], (-60.0, -30.0, -2.0, 2.0, 30.0, 60.0))
            - sidebands(spectra[0], centers[0], (-60.0, -30.0, -2.0, 2.0, 30.0, 60.0))))
    return alpha, artifacts, carrier


def _speaker_edge_modulation(
    quiet: np.ndarray, artifact: np.ndarray, combined: np.ndarray,
    start: int, stop: int, sample_rate_hz: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Derive FM sideband-equivalent amplitudes from speaker-current crossings."""
    phases = quiet.shape[0]
    alpha_index = np.empty(phases)
    artifact_index = np.empty(phases)
    carrier_rms = np.empty(phases)

    def frequency_tones(row: np.ndarray) -> dict[float, float]:
        segment = row[start:stop]
        crossing_indices = np.flatnonzero(segment[:-1]*segment[1:] < 0)
        require_count = 8
        if crossing_indices.size < require_count:
            return {frequency: 0.0 for frequency in (2.0, 10.0, 30.0, 60.0)}
        crossings = []
        for index in crossing_indices:
            left, right = segment[index], segment[index+1]
            crossings.append((start+index-left/(right-left))/sample_rate_hz)
        crossings = np.asarray(crossings)
        periods = crossings[2:]-crossings[:-2]
        times = (crossings[2:]+crossings[:-2])/2
        instantaneous = 1/periods
        instantaneous -= np.mean(instantaneous)
        result = {}
        for frequency in (2.0, 10.0, 30.0, 60.0):
            design = np.column_stack((np.sin(2*math.pi*frequency*times),
                                      np.cos(2*math.pi*frequency*times)))
            coefficients = np.linalg.lstsq(design, instantaneous, rcond=None)[0]
            result[frequency] = float(np.linalg.norm(coefficients))
        return result

    for phase in range(phases):
        quiet_tones = frequency_tones(quiet[phase])
        artifact_tones = frequency_tones(artifact[phase])
        combined_tones = frequency_tones(combined[phase])
        alpha_index[phase] = abs(combined_tones[10.0]-artifact_tones[10.0])/20.0
        artifact_index[phase] = math.sqrt(sum(
            ((artifact_tones[frequency]-quiet_tones[frequency])/(2*frequency))**2
            for frequency in (2.0, 30.0, 60.0)))
        carrier_rms[phase] = math.sqrt(float(np.mean(combined[phase, start:stop]**2)))
    return alpha_index*carrier_rms, artifact_index*carrier_rms, carrier_rms


def simulate_build(build: SonificationBuild, candidate: SonificationCandidate,
                   phase_steps: int, sample_rate_hz: float = 40_000.0,
                   duration_s: float = 1.8, alpha_on_s: float = 0.3,
                   alpha_off_s: float = 1.5, noise_seed: int = 0x48454144,
                   noise_enabled: bool = True) -> BuildResult:
    """Run every phase through silence/artifacts/alpha to speaker current."""
    if phase_steps < 1:
        raise ValueError("phase_steps must be positive")
    meas_tf, ref_tf = electrode_to_weighting(build, candidate)
    silence, artifacts, combined = electrode_stimulus(
        build, phase_steps, sample_rate_hz, duration_s, alpha_on_s, alpha_off_s,
        noise_seed, noise_enabled)
    silence_control = _filter(meas_tf, silence[0], sample_rate_hz) + _filter(
        ref_tf, silence[1], sample_rate_hz)
    artifact_control = _filter(meas_tf, artifacts[0], sample_rate_hz) + _filter(ref_tf, artifacts[1], sample_rate_hz)
    combined_control = _filter(meas_tf, combined[0], sample_rate_hz) + _filter(ref_tf, combined[1], sample_rate_hz)
    silence_control += build.vref_v
    artifact_control += build.vref_v
    combined_control += build.vref_v
    all_control = np.concatenate((silence_control, artifact_control, combined_control))
    control_upper = build.supply_v-build.oscillator_high_headroom_v
    control_margin = float(np.min(np.minimum(all_control-build.oscillator_low_v,
                                             control_upper-all_control)))
    control_slew = float(np.max(np.abs(np.diff(all_control, axis=1)))*sample_rate_hz)
    currents, edges, states, clipped, peak_current, margin = _speaker_current(
        all_control, build, sample_rate_hz)
    quiet = currents[:phase_steps]
    artifact_i = currents[phase_steps:2*phase_steps]
    combined_i = currents[2*phase_steps:]
    measurement = slice(round((alpha_on_s+0.4)*sample_rate_hz),
                        round((alpha_off_s-0.1)*sample_rate_hz))
    transitions = np.count_nonzero(edges[2*phase_steps:, measurement], axis=1)
    frequency = transitions/(2*((measurement.stop-measurement.start)/sample_rate_hz))
    alpha_rms, artifact_rms, carrier_rms = _speaker_edge_modulation(
        quiet, artifact_i, combined_i, measurement.start, measurement.stop,
        sample_rate_hz)
    t10, t90, offset = _settling_metrics(combined_control-artifact_control, sample_rate_hz,
                                         alpha_on_s, alpha_off_s)
    first_edge = []
    on_index = round(alpha_on_s*sample_rate_hz)
    for phase in range(phase_steps):
        different = np.flatnonzero(edges[phase_steps+phase, on_index:]
                                   != edges[2*phase_steps+phase, on_index:])
        first_edge.append(float(different[0]/sample_rate_hz) if different.size else duration_s-alpha_on_s)
    duty = np.mean(states[2*phase_steps:, measurement], axis=1)
    metrics = [PhaseMetrics(
        float(frequency[index]), float(duty[index]),
        float(np.sqrt(np.mean(combined_i[index, measurement]**2))),
        float(alpha_rms[index]), float(artifact_rms[index]),
        float(alpha_rms[index]/max(artifact_rms[index], 1e-15)),
        float(alpha_rms[index]/max(carrier_rms[index], 1e-15)), t10, t90, offset,
        first_edge[index], min(margin, control_margin), peak_current,
        clipped or control_slew > 0.3e6,
        bool(transitions[index] < 4),
    ) for index in range(phase_steps)]
    failures: list[str] = []
    for index, item in enumerate(metrics):
        if not 300 <= item.frequency_hz <= 1500: failures.append(f"phase {index}: frequency")
        if not 0.10 <= item.duty_cycle <= 0.90: failures.append(f"phase {index}: duty")
        if item.alpha_to_carrier < 0.01: failures.append(f"phase {index}: alpha modulation")
        if item.minimum_node_margin_v < 0.250: failures.append(f"phase {index}: node margin")
        if item.peak_lm386_current_a > 0.5: failures.append(f"phase {index}: amplifier current")
        if item.clipped: failures.append(f"phase {index}: clipping")
        if item.latched: failures.append(f"phase {index}: latching")
    worst = min(metrics, key=lambda item: (item.modulation_ratio, item.alpha_to_carrier,
                                            -item.onset_t90_s))
    return BuildResult(phase_steps, worst, failures[0] if failures else None)


def relaxation_frequency(r_feedback_ohm: float, c_timing_f: float,
                         low_v: float, high_v: float, threshold_low_v: float,
                         threshold_high_v: float) -> float:
    charge = r_feedback_ohm*c_timing_f*math.log((high_v-threshold_low_v)/(high_v-threshold_high_v))
    discharge = r_feedback_ohm*c_timing_f*math.log((threshold_high_v-low_v)/(threshold_low_v-low_v))
    return 1/(charge+discharge)
