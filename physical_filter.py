"""Physical candidate for the post-ALPHA two-stage MFB band-pass filter.

This is a circuit model, not a second schematic.  It names every proposed
part and solves the two MFB nodal equations with a finite single-pole op amp.
The candidate does not become authoritative hardware until the stress gate
passes and a later schematic checkpoint deliberately adds it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import cmath
import itertools
import math
from typing import Iterator


BOLTZMANN = 1.380649e-23


@dataclass(frozen=True)
class MfbStageParts:
    r1_ohm: float = 255_000.0
    r2_ohm: float = 64_900.0
    r5_ohm: float = 510_000.0
    c3_f: float = 100e-9
    c4_f: float = 100e-9

    @property
    def center_hz(self) -> float:
        numerator = self.r1_ohm + self.r2_ohm
        denominator = self.c3_f * self.c4_f * self.r1_ohm * self.r2_ohm * self.r5_ohm
        return math.sqrt(numerator / denominator) / (2 * math.pi)

    @property
    def q(self) -> float:
        numerator = math.sqrt(
            self.c3_f * self.c4_f * self.r1_ohm * self.r2_ohm * self.r5_ohm
            * (self.r1_ohm + self.r2_ohm)
        )
        return numerator / ((self.c3_f + self.c4_f) * self.r1_ohm * self.r2_ohm)

    @property
    def center_gain(self) -> float:
        return self.c3_f * self.r5_ohm / (self.r1_ohm * (self.c3_f + self.c4_f))


@dataclass(frozen=True)
class OpAmpModel:
    name: str = "LM358N"
    dc_open_loop_gain: float = 35_000.0
    gain_bandwidth_hz: float = 700_000.0
    input_offset_v: float = 7e-3
    input_bias_a: float = 150e-9
    input_voltage_noise_v_rt_hz: float = 40e-9
    flicker_noise_v_pp_01_10_hz: float = 3e-6
    input_current_noise_a_rt_hz: float = 0.2e-12
    slew_v_s: float = 0.3e6
    output_low_v: float = 0.020
    output_high_headroom_v: float = 2.0
    output_current_a: float = 5e-3
    common_mode_low_v: float = 0.0
    common_mode_high_headroom_v: float = 2.0
    overload_recovery_s: float = 50e-6


@dataclass(frozen=True)
class StageAcResult:
    frequency_hz: float
    transfer: complex
    summing_node_v_per_v: complex
    inverting_node_v_per_v: complex
    input_current_a_per_v: complex
    feedback_current_a_per_v: complex
    c4_current_a_per_v: complex
    output_current_a_per_v: complex


@dataclass(frozen=True)
class CascadeAcResult:
    stage1: StageAcResult
    stage2: StageAcResult

    @property
    def transfer(self) -> complex:
        return self.stage1.transfer * self.stage2.transfer


def _solve3(matrix: tuple[tuple[complex, ...], ...], rhs: tuple[complex, ...]) -> tuple[complex, ...]:
    rows = [list(row) + [value] for row, value in zip(matrix, rhs, strict=True)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-30:
            raise ValueError("singular physical-filter nodal matrix")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        scale = rows[column][column]
        rows[column] = [value / scale for value in rows[column]]
        for row in range(3):
            if row == column:
                continue
            scale = rows[row][column]
            rows[row] = [left - scale * right for left, right in zip(rows[row], rows[column], strict=True)]
    return tuple(rows[index][3] for index in range(3))


def solve_stage_ac(parts: MfbStageParts, opamp: OpAmpModel, frequency_hz: float) -> StageAcResult:
    """Solve summing node, op-amp input, output and branch currents for Vin=1."""
    if frequency_hz <= 0:
        raise ValueError("frequency must be positive")
    s = 2j * math.pi * frequency_hz
    aol_pole_hz = opamp.gain_bandwidth_hz / opamp.dc_open_loop_gain
    open_loop = opamp.dc_open_loop_gain / (1 + 1j * frequency_hz / aol_pole_hz)
    y1, y2, y5 = 1 / parts.r1_ohm, 1 / parts.r2_ohm, 1 / parts.r5_ohm
    y3, y4 = s * parts.c3_f, s * parts.c4_f
    x, minus, output = _solve3(
        (
            (y1 + y2 + y3 + y4, -y3, -y4),
            (-y3, y3 + y5, -y5),
            (0j, open_loop, 1 + 0j),
        ),
        (y1 + 0j, 0j, 0j),
    )
    input_current = (1 - x) * y1
    feedback_current = (output - minus) * y5
    c4_current = (output - x) * y4
    return StageAcResult(
        frequency_hz, output, x, minus, input_current, feedback_current,
        c4_current, feedback_current + c4_current,
    )


def solve_cascade_ac(
    stage1: MfbStageParts, stage2: MfbStageParts, opamp: OpAmpModel, frequency_hz: float
) -> CascadeAcResult:
    first = solve_stage_ac(stage1, opamp, frequency_hz)
    second = solve_stage_ac(stage2, opamp, frequency_hz)
    return CascadeAcResult(first, second)


def ideal_stage_transfer(parts: MfbStageParts, frequency_hz: float) -> complex:
    """Closed-form ideal-op-amp transfer used as an independent nodal oracle."""
    s = 2j * math.pi * frequency_hz
    numerator = -s * parts.c3_f * parts.r2_ohm * parts.r5_ohm
    denominator = (
        parts.c3_f * parts.c4_f * parts.r1_ohm * parts.r2_ohm * parts.r5_ohm * s * s
        + (parts.c3_f + parts.c4_f) * parts.r1_ohm * parts.r2_ohm * s
        + parts.r1_ohm + parts.r2_ohm
    )
    return numerator / denominator


def component_corner_cases(nominal: MfbStageParts | None = None) -> Iterator[tuple[int, MfbStageParts, MfbStageParts]]:
    """Yield all 2^10 independent endpoints for the two five-part stages."""
    nominal = nominal or MfbStageParts()
    names = ("r1_ohm", "r2_ohm", "r5_ohm", "c3_f", "c4_f")
    tolerances = (0.01, 0.01, 0.01, 0.05, 0.05)
    for coordinate, signs in enumerate(itertools.product((-1, 1), repeat=10)):
        stages = []
        for offset in (0, 5):
            stages.append(replace(nominal, **{
                name: getattr(nominal, name) * (1 + signs[offset + index] * tolerance)
                for index, (name, tolerance) in enumerate(zip(names, tolerances, strict=True))
            }))
        yield coordinate, stages[0], stages[1]


def integrated_output_noise_rms(
    first: MfbStageParts, second: MfbStageParts, opamp: OpAmpModel,
    temperature_k: float = 313.15, start_hz: float = 0.5, stop_hz: float = 100.0,
    points: int = 400,
) -> float:
    """Integrate conservative resistor, input-current, white and 1/f noise."""
    step = (stop_hz - start_hz) / (points - 1)
    psd = []
    flicker_density = opamp.flicker_noise_v_pp_01_10_hz / math.sqrt(10.0)
    for index in range(points):
        frequency = start_hz + index * step
        h1 = solve_stage_ac(first, opamp, frequency).transfer
        h2 = solve_stage_ac(second, opamp, frequency).transfer
        resistor_input = sum(4 * BOLTZMANN * temperature_k * resistance for resistance in (
            first.r1_ohm, first.r2_ohm, first.r5_ohm,
        )) / first.r1_ohm**2
        amplifier_input = (
            opamp.input_voltage_noise_v_rt_hz**2
            + (flicker_density / math.sqrt(max(frequency, 0.1)))**2
            + (opamp.input_current_noise_a_rt_hz * first.r1_ohm)**2
        )
        first_out = abs(h1 * h2) ** 2 * (resistor_input + amplifier_input)
        second_out = abs(h2) ** 2 * amplifier_input
        psd.append(first_out + second_out)
    variance = step * (0.5 * psd[0] + sum(psd[1:-1]) + 0.5 * psd[-1])
    return math.sqrt(variance)
