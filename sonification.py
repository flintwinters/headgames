"""Continuous-time EEG-to-speaker candidate and oscillator models.

The functions here contain no envelope, hold, or analysis-window state.  AC
metrics describe the causal analog weighting; the transient oscillator model
steps the existing U2D Schmitt relaxation topology and a bounded LM386 stage.
"""

from __future__ import annotations

from dataclasses import dataclass
import cmath
import math

from physical_filter import MfbStageParts, OpAmpModel, solve_stage_ac


@dataclass(frozen=True)
class SonificationCandidate:
    name: str
    mfb_stages: int
    physical_parts: int


@dataclass(frozen=True)
class CandidateMetrics:
    candidate: SonificationCandidate
    group_delay_s: float
    modulation_ratio: float
    distance: float = math.inf


@dataclass(frozen=True)
class OscillatorMetrics:
    frequency_hz: float
    duty_cycle: float
    carrier_peak_a: float
    sideband_peak_a: float
    speaker_rms_a: float
    harmonic_peak_a: tuple[float, ...]
    minimum_node_margin_v: float
    clipped: bool
    latched: bool


CANDIDATES = (
    SonificationCandidate("broadband", 0, 0),
    SonificationCandidate("alpha", 0, 4),
    SonificationCandidate("mfb1", 1, 11),
    SonificationCandidate("mfb2", 2, 22),
)


def mfb_multiplier(candidate: SonificationCandidate, frequency_hz: float,
                   parts: MfbStageParts | None = None) -> complex:
    """Return only the optional post-ALPHA weighting multiplier."""
    if candidate.mfb_stages == 0:
        return 1 + 0j
    stage = solve_stage_ac(parts or MfbStageParts(), OpAmpModel(), frequency_hz).transfer
    return stage ** candidate.mfb_stages


def group_delay(transfer, frequency_hz: float) -> float:
    """Numerically differentiate phase with a symmetric, unwrap-safe quotient."""
    delta = frequency_hz * 1e-4
    quotient = transfer(frequency_hz + delta) / transfer(frequency_hz - delta)
    return -cmath.phase(quotient) / (2 * math.pi * 2 * delta)


def worst_alpha_group_delay(transfer) -> float:
    return max(group_delay(transfer, 8.0 + index * 0.1) for index in range(41))


def pareto_knee(metrics: tuple[CandidateMetrics, ...]) -> tuple[CandidateMetrics, ...]:
    """Return the nondominated set with normalized ideal-point distances."""
    frontier = tuple(item for item in metrics if not any(
        (other.group_delay_s <= item.group_delay_s
         and other.modulation_ratio >= item.modulation_ratio
         and (other.group_delay_s < item.group_delay_s
              or other.modulation_ratio > item.modulation_ratio))
        for other in metrics
    ))
    delays = tuple(item.group_delay_s for item in frontier)
    ratios = tuple(item.modulation_ratio for item in frontier)
    delay_span = max(delays) - min(delays)
    ratio_span = max(ratios) - min(ratios)
    scored = tuple(CandidateMetrics(
        item.candidate, item.group_delay_s, item.modulation_ratio,
        math.hypot(
            0.0 if delay_span == 0 else (item.group_delay_s-min(delays))/delay_span,
            0.0 if ratio_span == 0 else (max(ratios)-item.modulation_ratio)/ratio_span,
        ),
    ) for item in frontier)
    return tuple(sorted(scored, key=lambda item: (
        item.distance, item.group_delay_s, item.candidate.physical_parts,
    )))


def relaxation_frequency(r_feedback_ohm: float, c_timing_f: float,
                         low_v: float, high_v: float, threshold_low_v: float,
                         threshold_high_v: float) -> float:
    """Closed-form frequency of the finite-swing RC relaxation oscillator."""
    charge = r_feedback_ohm*c_timing_f*math.log(
        (high_v-threshold_low_v)/(high_v-threshold_high_v))
    discharge = r_feedback_ohm*c_timing_f*math.log(
        (threshold_high_v-low_v)/(threshold_low_v-low_v))
    return 1/(charge+discharge)


def simulate_oscillator(control_peak_v: float, control_hz: float = 10.0,
                        duration_s: float = 0.5, sample_rate_hz: float = 100_000.0,
                        r_control_ohm: float = 220_000.0) -> OscillatorMetrics:
    """Step U2D, AC coupling, gain-20 LM386, and the 8-ohm speaker.

    U2D has 20 mV/7 V finite output swing.  The LM386 model has its official
    gain-20, 50 kohm input, and 300 kHz single-pole bandwidth, with output
    clipped conservatively to 1 V peak for the 8-ohm load.
    """
    dt = 1/sample_rate_hz
    low, high, vref = 0.020, 7.0, 4.5
    r3 = r4 = 220_000.0
    r9, c10 = 100_000.0, 10e-9
    r5, r8, rin = 1_000_000.0, 10_000.0, 50_000.0
    input_load = 1/(1/r8 + 1/rin)
    attenuation = input_load/(r5+input_load)
    lm_tau = 1/(2*math.pi*300_000.0)
    lm_alpha = 1-math.exp(-dt/lm_tau)
    output = high
    timing = vref
    audio = 0.0
    states: list[float] = []
    currents: list[float] = []
    minimum_margin = math.inf
    clipped = False
    transitions = 0
    previous = output
    for index in range(int(duration_s*sample_rate_hz)):
        time = index*dt
        control = vref + control_peak_v*math.sin(2*math.pi*control_hz*time)
        threshold = (vref/r3 + output/r4 + control/r_control_ohm) / (
            1/r3 + 1/r4 + 1/r_control_ohm)
        timing += (output-timing)*(1-math.exp(-dt/(r9*c10)))
        if output == high and timing >= threshold:
            output = low
        elif output == low and timing <= threshold:
            output = high
        transitions += output != previous
        previous = output
        target_audio = 20*attenuation*(output-(high+low)/2)
        audio += lm_alpha*(target_audio-audio)
        if abs(audio) > 1.0:
            clipped = True
            audio = max(-1.0, min(1.0, audio))
        minimum_margin = min(minimum_margin, threshold-low, high-threshold,
                             timing-low, high-timing)
        if index >= int(0.1*sample_rate_hz):
            states.append(output)
            currents.append(audio/8.0)
    window_s = duration_s-0.1
    frequency = transitions/(2*duration_s)
    duty = sum(value == high for value in states)/len(states)
    mean_i = sum(currents)/len(currents)
    rms = math.sqrt(sum(value*value for value in currents)/len(currents))
    def amplitude(frequency_hz: float) -> float:
        coefficient = sum((value-mean_i)*cmath.exp(-2j*math.pi*frequency_hz*(i/sample_rate_hz))
                          for i, value in enumerate(currents))
        return 2*abs(coefficient)/len(currents)
    carrier = amplitude(frequency)
    sideband = max(amplitude(frequency-control_hz), amplitude(frequency+control_hz))
    harmonics = tuple(amplitude(frequency*order) for order in range(1, 6))
    return OscillatorMetrics(frequency, duty, carrier, sideband, rms, harmonics,
                             minimum_margin, clipped, transitions < 4 or window_s <= 0)
