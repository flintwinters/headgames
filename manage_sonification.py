"""Project management implementation helpers."""

from manage_common import *  # noqa: F403
@lru_cache(maxsize=4)
def cached_mfb_synthesis(value_items: tuple[tuple[str, str], ...]):
    """Avoid repeating the deterministic inventory search per physical build."""
    values = dict(value_items)
    return synthesize_mfb(read_inventory(BOM, values))


def nominal_sonification_build(values: dict[str, str], electrode: str = "wet"
                               ) -> SonificationBuild:
    """Materialize every part exercised by the end-to-end transient."""
    synthesis = cached_mfb_synthesis(tuple(sorted(values.items())))
    stage = MfbParts(synthesis.r1.nominal, synthesis.r2.nominal,
                     synthesis.r5.nominal, 100e-9, 100e-9)
    profile = electrode_profile(electrode)
    mismatch = 1.10 if electrode == "wet" else 1.22
    meas = ChannelParts(profile.series_resistance_ohm,
                        profile.charge_transfer_resistance_ohm,
                        profile.interface_capacitance_f, 5e-12, 1_000_000.0, 40e-9,
                        resistance(values["R16"]), resistance(values["R14"]),
                        resistance(values["R15"]), capacitance(values["C12"]),
                        resistance(values["R12"]), capacitance(values["C11"]),
                        8_200.0, 150e-12)
    ref = ChannelParts(profile.series_resistance_ohm*mismatch,
                       profile.charge_transfer_resistance_ohm*mismatch,
                       profile.interface_capacitance_f/mismatch, 5e-12, 1_000_000.0, 40e-9,
                       resistance(values["R19"]), resistance(values["R20"]),
                       resistance(values["R21"]), capacitance(values["C14"]),
                       resistance(values["R22"]), capacitance(values["C15"]),
                       8_200.0, 250e-12)
    return SonificationBuild(
        meas, ref, resistance(values["R17"])+0.5*resistance(values["RV1"]),
        capacitance(values["C13"]),
        resistance(values["R23"])+0.5*resistance(values["RV2"]),
        capacitance(values["C16"]), (stage, stage),
        resistance(values["R3"]), resistance(values["R4"]),
        oscillator_control_resistance(values), resistance(values["R9"]),
        capacitance(values["C10"]), resistance(values["R5"]),
        resistance(values["R8"]), capacitance(values["C5"]),
        capacitance(values["C6"]), resistance(values["R10"]),
        capacitance(values["C7"]), 9.0, 4.5,
        0.020, 2.0, 20.0, 50_000.0, 300_000.0, 0.250, 8.0,
    )


def sampled_sonification_build(values: dict[str, str], electrode: str,
                               rng: random.Random) -> SonificationBuild:
    """Move every declared physical leaf independently for one build."""
    nominal = nominal_sonification_build(values, electrode)
    def move(value: float, tolerance: float) -> float:
        return value*(1+rng.uniform(-tolerance, tolerance))
    matched_input_scale = 1+rng.uniform(-0.01, 0.01)
    matched_feedback_scale = 1+rng.uniform(-0.01, 0.01)
    def channel(item: ChannelParts) -> ChannelParts:
        return replace(
            item,
            electrode_series_ohm=move(item.electrode_series_ohm, 0.20),
            electrode_charge_transfer_ohm=move(item.electrode_charge_transfer_ohm, 0.20),
            electrode_interface_f=move(item.electrode_interface_f, 0.20),
            buffer_input_f=move(item.buffer_input_f, 0.50),
            buffer_bandwidth_hz=rng.uniform(0.5e6, 2.0e6),
            buffer_noise_v_rt_hz=move(item.buffer_noise_v_rt_hz, 0.25),
            safety_a_ohm=move(item.safety_a_ohm, 0.05),
            safety_b_ohm=move(item.safety_b_ohm, 0.05),
            input_ohm=item.input_ohm*matched_input_scale,
            input_f=move(item.input_f, 0.10),
            feedback_ohm=item.feedback_ohm*matched_feedback_scale,
            feedback_f=move(item.feedback_f, 0.10),
            cable_isolation_ohm=move(item.cable_isolation_ohm, 0.05),
            cable_f=move(item.cable_f, 0.20),
        )
    synthesis = cached_mfb_synthesis(tuple(sorted(values.items())))
    stages = []
    for _ in range(2):
        stages.append(MfbParts(
            synthesis.r1.sample(rng), synthesis.r2.sample(rng),
            synthesis.r5.sample(rng), move(100e-9, 0.10), move(100e-9, 0.10),
        ))
    supply = rng.uniform(8.8, 9.2)
    return replace(
        nominal, meas=channel(nominal.meas), ref=channel(nominal.ref),
        alpha_input_ohm=move(nominal.alpha_input_ohm, 0.05),
        alpha_input_f=move(nominal.alpha_input_f, 0.10),
        alpha_feedback_ohm=move(nominal.alpha_feedback_ohm, 0.05),
        alpha_feedback_f=move(nominal.alpha_feedback_f, 0.10),
        mfb=tuple(stages), r3_ohm=move(nominal.r3_ohm, 0.05),
        r4_ohm=move(nominal.r4_ohm, 0.05),
        r6_ohm=(move(resistance(values["R6"]), 0.05)
                + 0.5*move(resistance(values["RV3"]), 0.05)),
        r9_ohm=move(nominal.r9_ohm, 0.01), c10_f=move(nominal.c10_f, 0.10),
        r5_audio_ohm=move(nominal.r5_audio_ohm, 0.05),
        r8_audio_ohm=move(nominal.r8_audio_ohm, 0.05),
        c5_audio_f=move(nominal.c5_audio_f, 0.10),
        c6_output_f=move(nominal.c6_output_f, 0.10),
        r10_zobel_ohm=move(nominal.r10_zobel_ohm, 0.05),
        c7_zobel_f=move(nominal.c7_zobel_f, 0.10),
        supply_v=supply, vref_v=supply/2+rng.uniform(-50e-3, 50e-3),
        oscillator_low_v=rng.uniform(0.005, 0.100),
        oscillator_high_headroom_v=rng.uniform(1.5, 2.5),
        lm386_input_ohm=move(nominal.lm386_input_ohm, 0.20),
        lm386_bandwidth_hz=move(nominal.lm386_bandwidth_hz, 0.20),
        lm386_output_power_w=rng.uniform(0.250, 0.325),
        speaker_ohm=move(nominal.speaker_ohm, 0.10),
    )


def print_sonification_result(candidate, result, build) -> None:
    item = result.worst
    delay = max(sonification_group_delay(build, candidate, frequency)
                for frequency in (8.0, 9.0, 10.0, 11.0, 12.0))
    table = Table(title=f"End-to-end speaker-current validation — {candidate.name}")
    table.add_column("Metric")
    table.add_column("Worst result", justify="right")
    table.add_row("Phases actually executed", str(result.phases_executed))
    table.add_row("Maximum 8-12 Hz group delay", f"{delay*1e3:.2f} ms")
    table.add_row("Alpha/artifact speaker-current ratio", f"{item.modulation_ratio:.4f}")
    table.add_row("Alpha modulation / carrier", f"{item.alpha_to_carrier:.2%}")
    table.add_row("Carrier / duty", f"{item.frequency_hz:.1f} Hz / {item.duty_cycle:.1%}")
    table.add_row("Burst onset t10 / t90", f"{item.onset_t10_s*1e3:.1f} / {item.onset_t90_s*1e3:.1f} ms")
    table.add_row("Offset to 10%", f"{item.offset_t10_s*1e3:.1f} ms")
    table.add_row("First changed carrier edge", f"{item.first_edge_latency_s*1e3:.1f} ms")
    table.add_row("Speaker RMS current", f"{item.speaker_rms_a*1e3:.2f} mA")
    table.add_row("LM386 peak output current", f"{item.peak_lm386_current_a*1e3:.2f} mA")
    table.add_row("Minimum node margin", f"{item.minimum_node_margin_v:.3f} V")
    table.add_row("Gate", result.first_failure or "PASS")
    console.print(table)


def verify_sonification_integrity(values: dict[str, str]) -> None:
    """Prove endpoint, phase, perturbation, and derivative contracts are real."""
    build = nominal_sonification_build(values, "wet")
    candidate = next(item for item in CANDIDATES if item.name == "alpha")
    result = simulate_build(build, candidate, 4, sample_rate_hz=40_000.0,
                            noise_seed=0x54455354, noise_enabled=False)
    refined = simulate_build(build, candidate, 4, sample_rate_hz=80_000.0,
                             noise_seed=0x54455354, noise_enabled=False)
    require(result.phases_executed == 4, "sonification phase count was not executed")
    require(result.worst.speaker_rms_a > 0,
            "sonification did not reach the speaker-current endpoint")
    require(result.worst.alpha_modulation_rms_a > 0,
            "alpha did not produce measured speaker-current modulation")
    require(abs(result.worst.frequency_hz-refined.worst.frequency_hz)
            / refined.worst.frequency_hz <= 0.025,
            f"carrier frequency is not converged: {result.worst.frequency_hz:.3f}/"
            f"{refined.worst.frequency_hz:.3f} Hz")
    require(abs(result.worst.duty_cycle-refined.worst.duty_cycle) <= 0.02,
            f"carrier duty is not converged: {result.worst.duty_cycle:.5f}/"
            f"{refined.worst.duty_cycle:.5f}")
    require(abs(result.worst.modulation_ratio-refined.worst.modulation_ratio)
            / max(refined.worst.modulation_ratio, 1e-15) <= 0.15,
            f"speaker modulation ratio is not converged: "
            f"{result.worst.modulation_ratio:.6f}/{refined.worst.modulation_ratio:.6f}")
    for frequency in (8.0, 10.0, 12.0):
        numerical = sonification_group_delay(build, candidate, frequency)
        exact = closed_form_group_delay(build, candidate, frequency)
        require(abs(numerical-exact) <= 1e-7,
                f"group-delay derivative mismatch at {frequency:g} Hz")
    left = sampled_sonification_build(values, "wet", random.Random(0x53454544))
    right = sampled_sonification_build(values, "wet", random.Random(0x53454544))
    require(left == right, "physical sonification build is not deterministic")
    require(left.meas.safety_a_ohm != left.meas.safety_b_ohm,
            "independent safety-resistor leaves moved together")
    require(left.meas.input_ohm/left.ref.input_ohm
            == build.meas.input_ohm/build.ref.input_ohm,
            "measured R15/R21 pair did not move together")
    require(left.meas.feedback_ohm/left.ref.feedback_ohm
            == build.meas.feedback_ohm/build.ref.feedback_ohm,
            "matched R12/R22 pair did not move together")
    require(left.meas.electrode_series_ohm != build.meas.electrode_series_ohm,
            "electrode spread was not exercised")
    require(left.meas.cable_f != build.meas.cable_f,
            "cable-capacitance spread was not exercised")
    require(left.lm386_output_power_w != build.lm386_output_power_w,
            "LM386 output-power bound was not exercised")
    require(left.speaker_ohm != build.speaker_ohm,
            "speaker-resistance spread was not exercised")
    require(left.mfb[0] != left.mfb[1],
            "independent MFB stages moved together")
    require(ALPHA_REDESIGN_R6_OHM == (100_000.0, 68_000.0, 47_000.0),
            "alpha-redesign R6 coverage changed")


def _sonification_frontier_case(arguments):
    """Picklable worker for one complete build/candidate/R6 experiment."""
    candidate, r6, base, phase_steps, seed, build_index = arguments
    r6_rng = random.Random(seed ^ (build_index*0x9E3779B1) ^ int(r6))
    build = replace(base, r6_ohm=r6*(1+r6_rng.uniform(-0.05, 0.05)))
    result = simulate_build(
        build, candidate, phase_steps,
        noise_seed=seed ^ (build_index*0x85EBCA6B),
    )
    delay = max(sonification_group_delay(build, candidate, 8+0.1*index)
                for index in range(41))
    return candidate.name, r6, build_index, result, delay


def _selected_spread_case(arguments):
    """Run one picklable selected-path build with deterministic endpoint noise."""
    build_index, build, candidate, phase_steps, seed = arguments
    result = simulate_build(
        build, candidate, phase_steps,
        noise_seed=seed ^ (build_index*0x85EBCA6B),
    )
    return build_index, result


def _alpha_redesign_case(arguments):
    """Run one candidate/R6/build combination for the redesign experiment."""
    candidate, r6, build_index, build, phase_steps, seed = arguments
    if build_index:
        rng = random.Random(seed ^ (build_index*0x9E3779B1) ^ int(r6))
        build = replace(build, r6_ohm=r6*(1+rng.uniform(-0.05, 0.05)))
    else:
        build = replace(build, r6_ohm=r6)
    result = simulate_build(
        build, candidate, phase_steps,
        noise_seed=seed ^ (build_index*0x85EBCA6B),
    )
    delay = max(sonification_group_delay(build, candidate, 8+0.1*index)
                for index in range(41))
    return candidate.name, r6, build_index, result, delay


def nominal_broadband_parts(feedback_ohm: float) -> BroadbandParts:
    """Materialize the proposed flat-gain and 60 Hz notch physical leaves."""
    return BroadbandParts(BROADBAND_GAIN_INPUT_OHM, feedback_ohm,
                          390_000.0, 390_000.0, 390_000.0, 390_000.0,
                          6.8e-9, 6.8e-9, 6.8e-9, 6.8e-9,
                          20_000.0, 620_000.0)


def broadband_build_from(build: SonificationBuild, nominal: SonificationBuild
                         ) -> SonificationBuild:
    """Retune the existing differential stage to gentle 1/30 Hz edges."""
    def channel(item: ChannelParts, reference: ChannelParts) -> ChannelParts:
        return replace(item,
                       input_f=330e-9*(item.input_f/reference.input_f),
                       feedback_f=510e-12*(item.feedback_f/reference.feedback_f))
    return replace(build, meas=channel(build.meas, nominal.meas),
                   ref=channel(build.ref, nominal.ref))


def sampled_broadband_parts(feedback_ohm: float, rng: random.Random
                            ) -> BroadbandParts:
    """Move every notch/gain physical leaf independently and deterministically."""
    nominal = nominal_broadband_parts(feedback_ohm)
    move = lambda value, tolerance: value*(1+rng.uniform(-tolerance, tolerance))
    return BroadbandParts(
        move(nominal.gain_input_ohm, 0.001),
        move(nominal.gain_feedback_ohm, 0.001),
        move(nominal.notch_r1_ohm, 0.001), move(nominal.notch_r2_ohm, 0.001),
        move(nominal.notch_r3a_ohm, 0.001), move(nominal.notch_r3b_ohm, 0.001),
        move(nominal.notch_c1_f, 0.001), move(nominal.notch_c2_f, 0.001),
        move(nominal.notch_c3a_f, 0.001), move(nominal.notch_c3b_f, 0.001),
        move(nominal.notch_q_set_ohm, 0.001),
        move(nominal.notch_q_feedback_ohm, 0.001),
    )


def _broadband_redesign_case(arguments):
    """Picklable complete-path worker with an explicit campaign identity."""
    feedback, r6, build_index, build, nominal, phase_steps, seed = arguments
    rng = random.Random(seed ^ (build_index*0x9E3779B1) ^ int(feedback) ^ int(r6))
    parts = (nominal_broadband_parts(feedback) if build_index == 0
             else sampled_broadband_parts(feedback, rng))
    physical = broadband_build_from(build, nominal)
    physical = replace(physical, r6_ohm=(r6 if build_index == 0
                                        else r6*(1+rng.uniform(-0.05, 0.05))))
    result = simulate_broadband_build(physical, parts, phase_steps)
    return feedback, r6, build_index, parts, result


def require_complete_broadband_campaign(
    identities: list[tuple[float, float, int]], expected: set[tuple[float, float, int]],
    results: list[BroadbandBuildResult], phase_steps: int,
) -> None:
    """Reject missing/duplicate cases and implicit or duplicated phase coverage."""
    require(len(identities) == len(set(identities)), "duplicate broadband campaign identity")
    require(set(identities) == expected, "missing or unexpected broadband campaign identity")
    for identity, result in zip(identities, results, strict=True):
        require(result.phase_ids == tuple(range(phase_steps)),
                f"{identity}: incomplete or duplicated phase identities")
        reported = {item.frequency_hz for item in result.frequencies}
        required = set(BROADBAND_WANTED_HZ+BROADBAND_SLOW_HZ+BROADBAND_REJECTION_HZ)
        require(reported == required, f"{identity}: incomplete frequency reporting")


def verify_broadband_integrity(values: dict[str, str]) -> None:
    """Durably prove topology math, independent leaves, and fail-closed coverage."""
    nominal = broadband_build_from(nominal_sonification_build(values, "wet"),
                                   nominal_sonification_build(values, "wet"))
    parts = nominal_broadband_parts(470_000.0)
    require(broadband_transfer_gain(nominal, parts, 10.0) > 0,
            "broadband path has zero transfer")
    require(broadband_transfer_gain(nominal, parts, 60.0)
            < broadband_transfer_gain(nominal, parts, 59.0),
            "60 Hz notch is not centered")
    require(broadband_group_delay(nominal, parts, 4.0) <= 0.030,
            "nominal broadband 4 Hz delay exceeds the declared gate")
    left = sampled_broadband_parts(470_000.0, random.Random(0x42524F41))
    right = sampled_broadband_parts(470_000.0, random.Random(0x42524F41))
    require(left == right, "broadband physical sampling is not deterministic")
    require(left.notch_r1_ohm != left.notch_r2_ohm,
            "independent notch resistors moved together")
    require(left.notch_c1_f != left.notch_c2_f,
            "independent notch capacitors moved together")
    require(left.notch_q_set_ohm != parts.notch_q_set_ohm
            and left.notch_q_feedback_ohm != parts.notch_q_feedback_ohm,
            "active notch Q-setting leaves did not move")
    fake_rows = tuple(
        # Endpoint values are irrelevant here; this fixture tests identities.
        BroadbandFrequencyResult(
            frequency, ("wanted" if frequency in BROADBAND_WANTED_HZ else
                        "slow AC only" if frequency in BROADBAND_SLOW_HZ else "rejection"),
            1.0, 0.0, None, None)
        for frequency in BROADBAND_WANTED_HZ+BROADBAND_SLOW_HZ+BROADBAND_REJECTION_HZ
    )
    fake = BroadbandBuildResult(tuple(range(4)), fake_rows, 700, 0.5, 1, 0, False, False, ())
    try:
        require_complete_broadband_campaign([(390e3, 47e3, 0), (390e3, 47e3, 0)],
                                            {(390e3, 47e3, 0)}, [fake, fake], 4)
    except VerificationError:
        pass
    else:
        raise VerificationError("duplicate campaign deliberately passed")
    incomplete = replace(fake, frequencies=fake.frequencies[:-1])
    try:
        require_complete_broadband_campaign([(390e3, 47e3, 0)],
                                            {(390e3, 47e3, 0)}, [incomplete], 4)
    except VerificationError:
        pass
    else:
        raise VerificationError("incomplete frequency report deliberately passed")

