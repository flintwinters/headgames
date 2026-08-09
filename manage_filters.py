"""Project management implementation helpers."""

from manage_common import *  # noqa: F403
def planned_sharper_filter() -> CascadedBandpass:
    """Return the planned two-biquad, unity-center-gain 8-12 Hz filter."""
    return CascadedBandpass.from_cutoffs(8.0, 12.0, stages=2)


def verify_physical_filter_synthesis() -> None:
    """Cross-check the proposed MFB parts, nodal solver, and ideal oracle."""
    parts = MfbStageParts()
    require(math.isclose(parts.center_hz, 9.79827727297, rel_tol=1e-10),
            f"physical MFB center changed: {parts.center_hz:.9f} Hz")
    require(math.isclose(parts.q, 1.56989199083, rel_tol=1e-10),
            f"physical MFB Q changed: {parts.q:.9f}")
    require(math.isclose(parts.center_gain, 1.0, rel_tol=1e-12),
            f"physical MFB center gain changed: {parts.center_gain:.9f}")
    oracle_opamp = OpAmpModel(dc_open_loop_gain=1e12, gain_bandwidth_hz=1e18)
    for frequency in (0.5, 2.0, 8.0, parts.center_hz, 12.0, 30.0, 100.0):
        nodal = solve_stage_ac(parts, oracle_opamp, frequency).transfer
        closed_form = ideal_stage_transfer(parts, frequency)
        require(abs(nodal - closed_form) <= 1e-9,
                f"MFB nodal/oracle disagreement at {frequency:g} Hz")
    physical = solve_stage_ac(parts, OpAmpModel(), parts.center_hz)
    require(abs(abs(physical.transfer) - 1.0) < 0.001,
            "finite-GBW LM358 model changes nominal center gain unexpectedly")
    require(abs(physical.summing_node_v_per_v) > 0.0,
            "physical solver did not expose the internal summing node")
    require(abs(physical.output_current_a_per_v) > 0.0,
            "physical solver did not expose source/load current")
    require(sum(1 for _ in component_corner_cases()) == 1_024,
            "physical filter must enumerate exactly 1,024 independent endpoints")
    require(FRONTIER_RESISTOR_BAND == 0.05,
            "unmarked resistors must use the specified 5% tolerance")
    require(FRONTIER_CAPACITOR_BAND == 0.10,
            "unmarked capacitors must use the specified 10% tolerance")
    left = bounded_stage_sample(random.Random(0x48454144), 0.05, 0.10)
    right = bounded_stage_sample(random.Random(0x48454144), 0.05, 0.10)
    require(left == right, "bounded Monte Carlo seed is not reproducible")


def verify_inventory_synthesis(values: dict[str, str]) -> None:
    """Prove BOM parsing, KiCad precedence, and deterministic network search."""
    inventory = read_inventory(BOM, values)
    require(bool(inventory), "inventory BOM contains no passive values")
    require(math.isclose(parse_compact_value("1.5n"), 1.5e-9, rel_tol=1e-15),
            "compact capacitor parsing changed")
    # The stale CSV comments for C11/C15 and C7 must never override KiCad.
    capacitor_values = {item.value for item in inventory if item.kind == "C"}
    require(capacitance(values["C11"]) in capacitor_values,
            "native C11 value did not override the stale BOM comment")
    require(capacitance(values["C7"]) in capacitor_values,
            "native C7 value did not override the stale BOM comment")
    first = synthesize_mfb(inventory)
    second = synthesize_mfb(inventory)
    require(first == second, "inventory MFB synthesis is not deterministic")
    require(first.part_count <= 14,
            "MFB synthesis exceeded four resistors per element")
    for network in (first.r1, first.r2, first.r5):
        low = network.endpoint((-1,) * network.part_count)
        high = network.endpoint((1,) * network.part_count)
        require(low < network.nominal < high,
                "physical-part tolerance endpoints were not propagated")


def print_inventory_synthesis(values: dict[str, str]) -> None:
    inventory = read_inventory(BOM, values)
    candidate = synthesize_mfb(inventory)
    table = Table(title="Inventory-aware MFB synthesis (per stage)")
    table.add_column("Element")
    table.add_column("Physical network")
    table.add_column("Effective value", justify="right")
    for name, network in (("R1", candidate.r1), ("R2", candidate.r2), ("R5", candidate.r5)):
        table.add_row(name, network.canonical, f"{network.nominal/1e3:.6g} kΩ")
    table.add_row("C3/C4", "one stocked 100 nF part each", "100 nF")
    table.add_row("f0 / Q / gain", "", f"{candidate.center_hz:.5f} Hz / {candidate.q:.5f} / {candidate.gain:.5f}")
    table.add_row("Physical parts", "", str(candidate.part_count))
    console.print(table)


def inventory_mfb_parts(values: dict[str, str], rng: random.Random | None = None) -> MfbStageParts:
    """Materialize one stage from the sole inventory-backed synthesis."""
    candidate = synthesize_mfb(read_inventory(BOM, values))
    if rng is None:
        return MfbStageParts(candidate.r1.nominal, candidate.r2.nominal,
                             candidate.r5.nominal, 100e-9, 100e-9)
    return MfbStageParts(
        candidate.r1.sample(rng), candidate.r2.sample(rng), candidate.r5.sample(rng),
        100e-9 * (1 + rng.uniform(-0.10, 0.10)),
        100e-9 * (1 + rng.uniform(-0.10, 0.10)),
    )


def sampled_inventory_mfb_parts(candidate, rng: random.Random) -> MfbStageParts:
    """Sample a pre-synthesized candidate without repeating the search."""
    return MfbStageParts(
        candidate.r1.sample(rng), candidate.r2.sample(rng), candidate.r5.sample(rng),
        100e-9 * (1 + rng.uniform(-0.10, 0.10)),
        100e-9 * (1 + rng.uniform(-0.10, 0.10)),
    )


def print_physical_filter_synthesis() -> None:
    parts, opamp = MfbStageParts(), OpAmpModel()
    table = Table(title="Candidate physical MFB filter — non-schematic")
    table.add_column("Quantity")
    table.add_column("Stage 1")
    table.add_column("Stage 2")
    table.add_row("R1 / R2 / R5", "255k / 64.9k / 510k", "255k / 64.9k / 510k")
    table.add_row("C3 / C4", "100n / 100n", "100n / 100n")
    table.add_row("Nominal f0", f"{parts.center_hz:.6f} Hz", f"{parts.center_hz:.6f} Hz")
    table.add_row("Nominal Q", f"{parts.q:.6f}", f"{parts.q:.6f}")
    table.add_row("Center gain", "−1.000 V/V", "−1.000 V/V")
    solved = solve_cascade_ac(parts, parts, opamp, parts.center_hz)
    table.add_row("Finite-op-amp cascade", f"{abs(solved.stage1.transfer):.6f}", f"{abs(solved.transfer):.6f}")
    table.add_row("Output current / Vin", f"{abs(solved.stage1.output_current_a_per_v)*1e6:.3f} µA/V", f"{abs(solved.stage2.output_current_a_per_v)*1e6:.3f} µA/V")
    console.print(table)
    console.print("[yellow]Candidate only:[/yellow] this network is not in headgames.kicad_sch.")


def _physical_filtered_outputs(
    outputs: tuple[tuple[float, complex], ...], first: MfbStageParts, second: MfbStageParts,
    opamp: OpAmpModel,
) -> tuple[tuple[float, complex], ...]:
    return tuple((frequency, output * solve_cascade_ac(first, second, opamp, frequency).transfer)
                 for frequency, output in outputs)


def _stress_metrics(
    values: dict[str, str], first: MfbStageParts, second: MfbStageParts,
    opamp: OpAmpModel, supply_v: float, detector_release_s: float,
    electrode: str,
) -> tuple[float, float, float]:
    artifacts = frontier_artifact_fixture_outputs(values, False, electrode)
    with_alpha = frontier_artifact_fixture_outputs(values, True, electrode)
    filtered_artifacts = _physical_filtered_outputs(artifacts, first, second, opamp)
    filtered_alpha = _physical_filtered_outputs(with_alpha, first, second, opamp)
    artifact_env = simulate_ideal_peak_detector(filtered_artifacts, detector_release_s,
                                          duration_seconds=2.0, sample_rate_hz=240.0,
                                          measurement_seconds=1.0)
    alpha_env = simulate_ideal_peak_detector(filtered_alpha, detector_release_s,
                                       duration_seconds=2.0, sample_rate_hz=240.0,
                                       measurement_seconds=1.0)
    change = (alpha_env.mean_v - artifact_env.mean_v) / artifact_env.mean_v
    stage1_peak = sum(abs(output * solve_stage_ac(first, opamp, frequency).transfer)
                      for frequency, output in with_alpha)
    stage2_peak = sum(abs(output) for _, output in _physical_filtered_outputs(
        with_alpha, first, second, opamp))
    vref = supply_v / 2
    upper = supply_v - opamp.output_high_headroom_v
    acquisition = eeg_path_model(values)
    # The AC-coupled 474 kohm input arms are open at DC, so offset sees unity
    # DC noise gain. Matched input bias currents cancel through the equal 10 Mohm
    # paths; the residual is set by input offset current rather than full bias.
    acquisition_dc_error = (
        LM324_ACQUISITION.input_offset_v
        + LM324_TYPICAL_INPUT_OFFSET_CURRENT_A
        * acquisition.diff_feedback_resistance
    )
    margin = (
        min(vref - LM324_ACQUISITION.output_low_v,
            supply_v - LM324_ACQUISITION.output_high_headroom_v - vref)
        - max(sum(abs(output) for _, output in with_alpha), stage1_peak, stage2_peak)
        - acquisition_dc_error
    )
    current = max(
        abs(output) * abs(solve_stage_ac(first, opamp, frequency).output_current_a_per_v)
        for frequency, output in with_alpha
    )
    return change, margin, current


def run_filter_stress(
    values: dict[str, str], tier: str, samples: int, seed: int,
    electrode: str = "wet",
) -> FilterStressResult:
    """Run the active-electrode frontier in a tight nominal operating band."""
    require(tier == "build", "only the nominal operating frontier is supported")
    require(samples >= 0, "samples must be non-negative")
    require(electrode in ("wet", "dry"), "electrode must be wet or dry")
    opamp = OpAmpModel()
    release = resistance(values["R18"]) * capacitance(values["C17"])
    minimum_change = math.inf
    minimum_margin = math.inf
    maximum_current = 0.0
    worst = "none"
    first_failure = None
    cases = 0

    def consume(label: str, first: MfbStageParts, second: MfbStageParts, supply: float) -> None:
        nonlocal minimum_change, minimum_margin, maximum_current, worst, first_failure, cases
        change, margin, current = _stress_metrics(
            values, first, second, opamp, supply, release, electrode
        )
        cases += 1
        if change < minimum_change or margin < minimum_margin:
            worst = label
        minimum_change = min(minimum_change, change)
        minimum_margin = min(minimum_margin, margin)
        maximum_current = max(maximum_current, current)
        failure = (
            f"{label}: alpha change {change:.1%}" if change < 0.25 else
            f"{label}: node margin {margin:.3f} V" if margin < 0.250 else
            f"{label}: output current {current*1e3:.3f} mA" if current > opamp.output_current_a else None
        )
        if failure is not None and first_failure is None:
            first_failure = failure

    candidate = synthesize_mfb(read_inventory(BOM, values))
    nominal = MfbStageParts(candidate.r1.nominal, candidate.r2.nominal,
                            candidate.r5.nominal, 100e-9, 100e-9)
    consume("nominal", nominal, nominal, FRONTIER_NOMINAL_SUPPLY_V)
    rng = random.Random(seed)
    for index in range(samples):
        supply = rng.uniform(
            FRONTIER_NOMINAL_SUPPLY_V - FRONTIER_SUPPLY_BAND_V,
            FRONTIER_NOMINAL_SUPPLY_V + FRONTIER_SUPPLY_BAND_V,
        )
        first = sampled_inventory_mfb_parts(candidate, rng)
        second = sampled_inventory_mfb_parts(candidate, rng)
        consume(f"near-nominal:{index:05d}", first, second, supply)

    noise = integrated_output_noise_rms(nominal, nominal, opamp)
    alpha_peak = abs(frontier_artifact_fixture_outputs(values, True, electrode)[-1][1]) * abs(
        solve_cascade_ac(nominal, nominal, opamp, 10.0).transfer
    )
    recovery = recovery_bound_seconds(nominal, opamp, release)
    detector_artifacts = simulate_precision_peak_detector(
        _physical_filtered_outputs(
            frontier_artifact_fixture_outputs(values, False, electrode), nominal, nominal, opamp
        ),
        resistance(values["R18"]), capacitance(values["C17"]), 9.0, 4.5,
        LM358_DETECTOR, DETECTOR_DIODE, duration_seconds=3.0,
        sample_rate_hz=2_000.0, measurement_seconds=1.0,
    )
    detector_alpha = simulate_precision_peak_detector(
        _physical_filtered_outputs(
            frontier_artifact_fixture_outputs(values, True, electrode), nominal, nominal, opamp
        ),
        resistance(values["R18"]), capacitance(values["C17"]), 9.0, 4.5,
        LM358_DETECTOR, DETECTOR_DIODE, duration_seconds=3.0,
        sample_rate_hz=2_000.0, measurement_seconds=1.0,
    )
    physical_change = (
        detector_alpha.envelope.mean_v - detector_artifacts.envelope.mean_v
    ) / detector_artifacts.envelope.mean_v
    minimum_change = min(minimum_change, physical_change)
    minimum_margin = min(
        minimum_margin,
        detector_artifacts.minimum_output_margin_v,
        detector_alpha.minimum_output_margin_v,
        detector_artifacts.minimum_common_mode_margin_v,
        detector_alpha.minimum_common_mode_margin_v,
    )
    maximum_current = max(
        maximum_current,
        detector_artifacts.peak_output_current_a,
        detector_alpha.peak_output_current_a,
    )
    if (detector_artifacts.clipped_samples or detector_alpha.clipped_samples) and first_failure is None:
        first_failure = (
            "nominal detector: LM358 diode-drive output reaches its declared "
            "swing limit during normal rectifier operation"
        )
    if min(detector_artifacts.minimum_common_mode_margin_v,
           detector_alpha.minimum_common_mode_margin_v) < 0.250 and first_failure is None:
        first_failure = "nominal detector: common-mode margin is below 250 mV"
    if noise >= alpha_peak * 0.10 and first_failure is None:
        first_failure = f"noise {noise:.6g} V exceeds {alpha_peak*0.10:.6g} V"
    if recovery > 2.0 and first_failure is None:
        first_failure = f"recovery bound {recovery:.3f} s exceeds 2 s"
    return FilterStressResult(tier, cases, worst, minimum_change, minimum_margin,
                              maximum_current, noise, alpha_peak * 0.10, recovery,
                              first_failure)


