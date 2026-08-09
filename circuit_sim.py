"""Small-signal circuit simulation for the analog EEG acquisition path.

The model is intentionally expressed in impedances matching the schematic's
two active stages.  It is not a second schematic: component values are always
supplied by ``manage.py`` after extraction from the native KiCad schematic.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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


@dataclass(frozen=True)
class ActiveElectrodeChannel:
    """Candidate unity-buffer and cable parameters for one electrode."""

    amplifier_name: str
    electrode_resistance: float
    input_capacitance: float
    gain_bandwidth_hz: float
    output_resistance: float
    cable_capacitance: float
    input_bias_current: float
    white_voltage_noise: float
    electrode_series_resistance: float | None = None
    charge_transfer_resistance: float | None = None
    interface_capacitance: float | None = None


@dataclass(frozen=True)
class ElectrodeProfile:
    """Randles-style electrode interface plus deterministic artifact terms."""

    name: str
    series_resistance_ohm: float
    charge_transfer_resistance_ohm: float
    interface_capacitance_f: float
    half_cell_offset_v: float
    drift_peak_v: float
    motion_peak_v: float
    excess_noise_v_rt_hz_at_1hz: float

    def impedance(self, frequency_hz: float) -> complex:
        charge_transfer = parallel(
            complex(self.charge_transfer_resistance_ohm),
            capacitor_impedance(self.interface_capacitance_f, frequency_hz),
        )
        return self.series_resistance_ohm + charge_transfer


@dataclass(frozen=True)
class BufferStability:
    phase_margin_deg: float
    overshoot_fraction: float
    settling_seconds: float


def electrode_profile(name: str) -> ElectrodeProfile:
    """Return the declared wet gating or dry informational interface."""
    profiles = {
        "wet": ElectrodeProfile("wet", 5_000.0, 25_000.0, 0.50e-6,
                                0.150, 0.5e-3, 1.0e-3, 80e-9),
        "dry": ElectrodeProfile("dry", 11_000.0, 60_000.0, 0.40e-6,
                                0.250, 1.0e-3, 2.0e-3, 160e-9),
    }
    try:
        return profiles[name]
    except KeyError as error:
        raise ValueError(f"unknown electrode profile: {name}") from error


def follower_cable_stability(
    gain_bandwidth_hz: float, isolation_resistance_ohm: float,
    cable_capacitance_f: float,
) -> BufferStability:
    """Conservative two-pole unity-follower cable-load estimate."""
    load_pole_hz = 1 / (2 * math.pi * isolation_resistance_ohm * cable_capacitance_f)
    phase_margin = 90.0 - math.degrees(math.atan(gain_bandwidth_hz / load_pole_hz))
    damping = max(0.05, min(1.0, phase_margin / 90.0))
    overshoot = math.exp(-math.pi * damping / math.sqrt(max(1e-12, 1-damping*damping))) if damping < 1 else 0.0
    settling = 4 / (2 * math.pi * gain_bandwidth_hz * damping)
    return BufferStability(phase_margin, overshoot, settling)


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


@dataclass(frozen=True)
class CascadedBandpass:
    """Identical unity-center-gain second-order band-pass sections."""

    center_frequency_hz: float
    section_q: float
    stages: int

    @classmethod
    def from_cutoffs(
        cls, low_hz: float, high_hz: float, stages: int = 2
    ) -> CascadedBandpass:
        """Synthesize equal-Q sections with -3 dB at reciprocal cutoffs."""
        if low_hz <= 0 or high_hz <= low_hz or stages < 1:
            raise ValueError("invalid band-pass specification")
        center = math.sqrt(low_hz * high_hz)
        frequency_ratio = low_hz / center
        section_magnitude_squared = 2 ** (-1 / stages)
        reactance_term = 1 - frequency_ratio**2
        normalized_numerator = math.sqrt(
            section_magnitude_squared
            * reactance_term**2
            / (1 - section_magnitude_squared)
        )
        section_q = frequency_ratio / normalized_numerator
        return cls(center, section_q, stages)

    def transfer(
        self,
        frequency_hz: float,
        center_scales: tuple[float, ...] | None = None,
        q_scales: tuple[float, ...] | None = None,
    ) -> complex:
        """Return cascade transfer with optional per-section coefficient errors."""
        center_scales = center_scales or (1.0,) * self.stages
        q_scales = q_scales or (1.0,) * self.stages
        if len(center_scales) != self.stages or len(q_scales) != self.stages:
            raise ValueError("one center and Q scale is required per section")
        result = 1 + 0j
        for center_scale, q_scale in zip(center_scales, q_scales, strict=True):
            normalized = (
                1j * frequency_hz / (self.center_frequency_hz * center_scale)
            )
            section_q = self.section_q * q_scale
            result *= (normalized / section_q) / (
                normalized**2 + normalized / section_q + 1
            )
        return result


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
    meas_electrode_resistance: complex,
    ref_electrode_resistance: complex,
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


def simulate_nonideal_electrode_inputs(
    parts: EegPathComponents,
    frequency_hz: float,
    meas_peak_v: complex,
    ref_peak_v: complex,
    meas_electrode_resistance: complex,
    ref_electrode_resistance: complex,
    amplifier: AmplifierLimits,
) -> complex:
    """Apply finite A0/GBW LM324 closed-loop error to the exact nodal path.

    The passive topology remains the full solution in ``simulate_electrode_inputs``.
    Each active stage is then corrected by its frequency-dependent loop gain and
    physical noise gain. DC offset and bias are handled as headroom/error terms in
    the transient gate because the schematic coupling capacitors reject DC.
    """
    ideal = simulate_electrode_inputs(
        parts, frequency_hz, meas_peak_v, ref_peak_v,
        meas_electrode_resistance, ref_electrode_resistance,
    )
    pole_hz = amplifier.gain_bandwidth_hz / amplifier.dc_open_loop_gain
    open_loop = amplifier.dc_open_loop_gain / (1 + 1j * frequency_hz / pole_hz)
    diff_input = (
        max(abs(meas_electrode_resistance), abs(ref_electrode_resistance))
        + parts.safety_resistance + parts.input_resistance
        + abs(capacitor_impedance(parts.input_capacitance, frequency_hz))
    )
    diff_feedback = abs(parallel(
        complex(parts.diff_feedback_resistance),
        capacitor_impedance(parts.diff_feedback_capacitance, frequency_hz),
    ))
    alpha_input = parts.alpha_input_resistance + abs(
        capacitor_impedance(parts.alpha_input_capacitance, frequency_hz)
    )
    alpha_feedback = abs(parallel(
        complex(parts.alpha_feedback_resistance),
        capacitor_impedance(parts.alpha_feedback_capacitance, frequency_hz),
    ))
    diff_noise_gain = 1 + diff_feedback / diff_input
    alpha_noise_gain = 1 + alpha_feedback / alpha_input
    return ideal * open_loop / (open_loop + diff_noise_gain) * open_loop / (
        open_loop + alpha_noise_gain
    )


def active_electrode_thevenin(
    channel: ActiveElectrodeChannel,
    safety_resistance: float,
    frequency_hz: float,
    electrode_peak_v: complex,
) -> tuple[complex, complex]:
    """Return the buffered cable end as a Thevenin source and impedance.

    The safety resistance remains physically electrode-side of the buffer.  Its
    interaction with buffer input capacitance is included.  The output model
    includes a one-pole follower response and cable capacitance driven through
    finite output resistance.
    """
    angular_frequency = 2 * math.pi * frequency_hz
    electrode_impedance = complex(channel.electrode_resistance)
    if (channel.electrode_series_resistance is not None
            and channel.charge_transfer_resistance is not None
            and channel.interface_capacitance is not None):
        electrode_impedance = channel.electrode_series_resistance + parallel(
            complex(channel.charge_transfer_resistance),
            capacitor_impedance(channel.interface_capacitance, frequency_hz),
        )
    input_pole = 1 / (
        1
        + 1j
        * angular_frequency
        * (electrode_impedance + safety_resistance)
        * channel.input_capacitance
    )
    follower_pole = 1 / (1 + 1j * frequency_hz / channel.gain_bandwidth_hz)
    cable_factor = 1 / (
        1
        + 1j
        * angular_frequency
        * channel.output_resistance
        * channel.cable_capacitance
    )
    return (
        electrode_peak_v * input_pole * follower_pole * cable_factor,
        channel.output_resistance * cable_factor,
    )


def simulate_active_electrode_inputs(
    parts: EegPathComponents,
    frequency_hz: float,
    meas_peak_v: complex,
    ref_peak_v: complex,
    meas_channel: ActiveElectrodeChannel,
    ref_channel: ActiveElectrodeChannel,
) -> complex:
    """Solve ALPHA when unity buffers drive the central acquisition network."""
    meas_source, meas_impedance = active_electrode_thevenin(
        meas_channel,
        parts.safety_resistance,
        frequency_hz,
        meas_peak_v,
    )
    ref_source, ref_impedance = active_electrode_thevenin(
        ref_channel,
        parts.safety_resistance,
        frequency_hz,
        ref_peak_v,
    )
    central_parts = replace(parts, electrode_resistance=0.0, safety_resistance=0.0)
    return simulate_electrode_inputs(
        central_parts,
        frequency_hz,
        meas_source,
        ref_source,
        meas_impedance,
        ref_impedance,
    )


def simulate_nonideal_active_electrode_inputs(
    parts: EegPathComponents,
    frequency_hz: float,
    meas_peak_v: complex,
    ref_peak_v: complex,
    meas_channel: ActiveElectrodeChannel,
    ref_channel: ActiveElectrodeChannel,
    amplifier: AmplifierLimits,
) -> complex:
    """Solve electrode-site buffers through the finite-A0/GBW acquisition path.

    Both independent safety resistors are included in the source impedance on
    the body side of each buffer. The cable capacitance is therefore driven by
    the buffer output rather than directly by the electrode impedance.
    """
    meas_source, meas_impedance = active_electrode_thevenin(
        meas_channel,
        parts.safety_resistance,
        frequency_hz,
        meas_peak_v,
    )
    ref_source, ref_impedance = active_electrode_thevenin(
        ref_channel,
        parts.safety_resistance,
        frequency_hz,
        ref_peak_v,
    )
    central_parts = replace(parts, electrode_resistance=0.0, safety_resistance=0.0)
    return simulate_nonideal_electrode_inputs(
        central_parts,
        frequency_hz,
        meas_source,
        ref_source,
        meas_impedance,
        ref_impedance,
        amplifier,
    )


def active_electrode_output_noise_rms(
    parts: EegPathComponents,
    meas_channel: ActiveElectrodeChannel,
    ref_channel: ActiveElectrodeChannel,
    start_hz: float = 0.5,
    stop_hz: float = 100.0,
    points: int = 2_001,
    temperature_k: float = 300.0,
) -> float:
    """Integrate declared white buffer and source-resistance noise at ALPHA.

    The two channels are treated as uncorrelated. This deliberately excludes
    buffer 1/f noise, current noise, central-amplifier noise, and interference.
    """
    boltzmann = 1.380649e-23
    meas_density_squared = meas_channel.white_voltage_noise**2 + (
        4
        * boltzmann
        * temperature_k
        * (meas_channel.electrode_resistance + parts.safety_resistance)
    )
    ref_density_squared = ref_channel.white_voltage_noise**2 + (
        4
        * boltzmann
        * temperature_k
        * (ref_channel.electrode_resistance + parts.safety_resistance)
    )
    differential_density_squared = meas_density_squared + ref_density_squared
    spacing = (stop_hz - start_hz) / (points - 1)
    output_psd: list[float] = []
    for index in range(points):
        frequency = start_hz + index * spacing
        gain = simulate_active_electrode_inputs(
            parts,
            frequency,
            0.5,
            -0.5,
            meas_channel,
            ref_channel,
        )
        output_psd.append(abs(gain) ** 2 * differential_density_squared)
    variance = spacing * (
        0.5 * output_psd[0] + sum(output_psd[1:-1]) + 0.5 * output_psd[-1]
    )
    return math.sqrt(variance)


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
        # A precision peak detector intentionally parks the diode drive at the
        # low rail while the diode is off. Only upper-rail contact during a
        # charging excursion represents lost signal headroom.
        if unclipped_drive > upper_output:
            clipped_samples += 1
            recovery_remaining = max(recovery_remaining, amplifier.overload_recovery_s)

        forward_current = max(
            0.0, (drive - held - diode.forward_drop_v) / diode.series_resistance_ohm
        )
        forward_current = min(forward_current, amplifier.output_current_a)
        release_current = max(0.0, (held - vref_v) / release_resistance_ohm)
        bias_current = amplifier.input_bias_a
        held += dt * (
            forward_current - release_current - diode.reverse_leakage_a - bias_current
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
