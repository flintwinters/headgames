"""Project management implementation helpers."""

from manage_common import *  # noqa: F403
def require_spice_models() -> None:
    """Fail closed when ngspice or either source-locked model is absent."""
    require(shutil.which("ngspice") is not None,
            "ngspice is required for circuit-level verification but was not found")
    expected = {
        PROJECT_ROOT / "models" / "ti" / "lmx58_lm2904.lib":
            "467a3e573420d1f5a21fab57b76be0e13073e854f609a73459a191958e314726",
        PROJECT_ROOT / "models" / "compat" / "lmx24_lmx58_nominal.lib":
            "8334c31c9a13f76d63232295e2d2ad73c5d0f99c17f30a5adc5ba68335ccb3d8",
    }
    for path, digest in expected.items():
        require(path.is_file(), f"required SPICE model is missing: {path.relative_to(PROJECT_ROOT)}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        require(actual == digest,
                f"SPICE model hash is not locked or mismatched: {path.name}")


def generate_filter_spice_deck() -> Path:
    """Generate the candidate-only cross-check deck under build/spice."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    output_dir.mkdir(parents=True, exist_ok=True)
    model = (
        PROJECT_ROOT / "models" / "compat" / "lmx24_lmx58_nominal.lib"
    ).resolve()
    deck = output_dir / "physical_filter_ac.cir"
    deck.write_text(f"""Headgames generated physical MFB AC cross-check
.include {model}
VCC vcc 0 9
VREF vref 0 4.5
VIN in vref dc 0 ac 1
R1A in x1 255k
R2A x1 vref 64.9k
C3A x1 n1 100n
C4A out1 x1 100n
R5A out1 n1 510k
XFA vref n1 vcc 0 out1 HG_LMX24_LMX58_NOMINAL
R1B out1 x2 255k
R2B x2 vref 64.9k
C3B x2 n2 100n
C4B out2 x2 100n
R5B out2 n2 510k
XFB vref n2 vcc 0 out2 HG_LMX24_LMX58_NOMINAL
.ac dec 1000 0.5 100
.print ac vm(out2) vp(out2)
.end
""", encoding="utf-8")
    return deck


def spice_nominal_model_dc_crosscheck() -> float:
    """Verify offset-current cancellation in a matched 10 Mohm DC fixture."""
    require_spice_models()
    output_dir = PROJECT_ROOT / "build" / "spice"
    output_dir.mkdir(parents=True, exist_ok=True)
    model = (
        PROJECT_ROOT / "models" / "compat" / "lmx24_lmx58_nominal.lib"
    ).resolve()
    deck = output_dir / "nominal_model_dc.cir"
    deck.write_text(f"""Headgames nominal op-amp DC contract
.include {model}
VCC vcc 0 9
VREF vref 0 4.5
RPLUS plus vref 10meg
RFB out minus 10meg
XU plus minus vcc 0 out HG_LMX24_LMX58_NOMINAL
.op
.print op v(plus) v(minus) v(out)
.end
""", encoding="utf-8")
    completed = subprocess.run(
        ["ngspice", "-b", str(deck)], cwd=deck.parent,
        check=False, capture_output=True, text=True,
    )
    require(completed.returncode == 0,
            f"ngspice DC contract failed: {completed.stderr[-500:]}")
    rows = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[0].isdigit():
            try:
                rows.append(tuple(float(value) for value in fields[1:]))
            except ValueError:
                continue
    require(len(rows) == 1, "ngspice DC contract produced no unique operating point")
    plus, minus, output = rows[0]
    require(abs(abs(plus - minus) - LM324_ACQUISITION.input_offset_v) <= 100e-6,
            f"nominal model input offset is {plus-minus:.6g} V")
    dc_error = output - 4.5
    expected = (
        LM324_ACQUISITION.input_offset_v
        + LM324_TYPICAL_INPUT_OFFSET_CURRENT_A * 10_000_000.0
    )
    require(abs(abs(dc_error) - expected) <= 10e-3,
            f"nominal model DC error {dc_error:.6g} V differs from {expected:.6g} V")
    return dc_error


def spice_ti_dc_characterization() -> tuple[float, float]:
    """Instantiate TI's model through ngspice's PSpice compatibility frontend."""
    require_spice_models()
    output_dir = PROJECT_ROOT / "build" / "spice"
    output_dir.mkdir(parents=True, exist_ok=True)
    deck = output_dir / "ti_model_dc.cir"
    deck.write_text(f"""Headgames TI LM358 DC characterization
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VIN plus 0 4.5
XU plus out vcc 0 out LMX58_LM2904
RL out 0 10k
.op
.print op v(plus) v(out) i(VCC)
.end
""", encoding="utf-8")
    completed = subprocess.run(
        ["ngspice", "-D", "ngbehavior=ps", "-b", str(deck)], cwd=deck.parent,
                               check=False, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    require(completed.returncode == 0,
            f"TI model rejected by ngspice PSpice compatibility: {(completed.stderr or completed.stdout)[-800:]}")
    rows = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[0].isdigit():
            try:
                rows.append(tuple(float(value) for value in fields[1:]))
            except ValueError:
                continue
    require(len(rows) == 1, "TI DC characterization produced no operating point")
    input_v, output_v, supply_a = rows[0]
    require(abs(output_v - input_v) <= 20e-3,
            f"TI follower DC error is {output_v-input_v:.6g} V")
    quiescent_a = abs(supply_a) - output_v / 10_000.0
    require(0.1e-3 <= quiescent_a <= 1e-3,
            f"TI quiescent supply current is {quiescent_a:.6g} A")
    return output_v - input_v, quiescent_a


def _run_ti_spice(deck: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["ngspice", "-D", "ngbehavior=ps", "-b", str(deck)], cwd=deck.parent,
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    require(completed.returncode == 0,
            f"TI PSpice compatibility run failed: {(completed.stderr or completed.stdout)[-800:]}")
    return completed


def _spice_table(stdout: str, columns: int) -> list[tuple[float, ...]]:
    rows: list[tuple[float, ...]] = []
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) == columns + 1 and fields[0].isdigit():
            try:
                rows.append(tuple(float(value) for value in fields[1:]))
            except ValueError:
                continue
    return rows


def spice_ti_ac_characterization() -> tuple[float, float, float]:
    """Measure the native TI model's follower gain, phase, and bandwidth."""
    require_spice_models()
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / "ti_follower_ac.cir"
    deck.write_text(f"""Headgames TI LM358 AC characterization
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VIN plus 0 dc 4.5 ac 1
XU plus out vcc 0 out LMX58_LM2904
RL out 0 10k
.ac dec 40 1 10meg
.print ac vm(out) vp(out)
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 3)
    require(rows, "TI AC characterization produced no frequency points")
    low_frequency, low_gain, low_phase_rad = rows[0]
    require(0.98 <= low_gain <= 1.02, f"TI follower LF gain is {low_gain:.6g}")
    low_phase_deg = math.degrees(low_phase_rad)
    require(abs(low_phase_deg) <= 2.0, f"TI follower LF phase is {low_phase_deg:.3f} degrees")
    target = low_gain / math.sqrt(2)
    bandwidth = min(rows, key=lambda row: abs(row[1] - target))[0]
    require(0.5e6 <= bandwidth <= 2.0e6,
            f"TI follower -3 dB bandwidth is {bandwidth:.6g} Hz")
    return low_gain, low_phase_deg, bandwidth


def spice_ti_bias_characterization() -> float:
    """Measure input bias current from a source-resistor voltage drop."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / "ti_bias_dc.cir"
    deck.write_text(f"""Headgames TI LM358 input-bias characterization
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VBIAS bias 0 4.5
RSOURCE bias plus 1meg
XU plus out vcc 0 out LMX58_LM2904
RL out 0 10k
.op
.print op v(bias) v(plus)
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 2)
    require(len(rows) == 1, "TI bias characterization produced no operating point")
    bias_v, plus_v = rows[0]
    current = abs(bias_v - plus_v) / 1e6
    require(5e-9 <= current <= 100e-9, f"TI input bias current is {current:.6g} A")
    return current


def spice_ti_slew_characterization() -> float:
    """Measure large-signal follower slew between 10% and 90% levels."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / "ti_slew_tran.cir"
    deck.write_text(f"""Headgames TI LM358 slew characterization
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VIN plus 0 pulse(2 6 1m 10n 10n 4m 10m)
XU plus out vcc 0 out LMX58_LM2904
RL out 0 10k
.tran 1u 4m
.print tran v(out)
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 2)
    post = [(time, voltage) for time, voltage in rows if time >= 1e-3]
    require(post, "TI slew characterization produced no transient samples")
    t10 = next((time for time, voltage in post if voltage >= 2.4), None)
    t90 = next((time for time, voltage in post if voltage >= 5.6), None)
    require(t10 is not None and t90 is not None and t90 > t10,
            "TI slew characterization did not cross 10% and 90%")
    slew = 3.2 / (t90 - t10)
    require(0.1e6 <= slew <= 1.0e6, f"TI positive slew rate is {slew:.6g} V/s")
    return slew


def spice_ti_swing_characterization() -> tuple[float, float]:
    """Check loaded follower tracking across its declared common-mode range."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / "ti_swing_dc.cir"
    deck.write_text(f"""Headgames TI LM358 loaded output-swing characterization
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VIN plus 0 0.1
XU plus out vcc 0 out LMX58_LM2904
RL out 0 10k
.dc VIN 0.1 7.0 0.1
.print dc v(plus) v(out)
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 3)
    require(rows, "TI swing characterization produced no sweep points")
    _, low_in, low_out = rows[0]
    _, high_in, high_out = rows[-1]
    require(0 <= low_out <= high_out <= 9.0, "TI loaded output escaped its rails")
    require(abs(low_out-low_in) <= 50e-3, f"TI low output tracking error is {low_out-low_in:.6g} V")
    require(abs(high_out-high_in) <= 100e-3, f"TI high output tracking error is {high_out-high_in:.6g} V")
    return low_out, high_out


def spice_ti_noise_characterization() -> float:
    """Integrate TI-model unity-follower output noise over 0.5–100 Hz."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / "ti_noise.cir"
    deck.write_text(f"""Headgames TI LM358 noise characterization
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VIN plus 0 dc 4.5 ac 1
XU plus out vcc 0 out LMX58_LM2904
RL out 0 10k
.noise v(out) VIN dec 40 0.5 100
.print noise onoise_spectrum
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 2)
    require(len(rows) >= 2, "TI noise characterization produced no spectrum")
    variance = 0.0
    for (left_f, left_n), (right_f, right_n) in zip(rows, rows[1:]):
        variance += (right_f-left_f) * (left_n*left_n + right_n*right_n) / 2
    noise = math.sqrt(max(0.0, variance))
    require(0 < noise <= 100e-6, f"TI integrated 0.5-100 Hz noise is {noise:.6g} V RMS")
    return noise


def spice_ti_cable_transient(cable_pf: float = 250.0,
                             isolation_ohm: float = 100.0) -> tuple[float, float]:
    """Measure step overshoot and 2% settling through the physical 100 ohm isolator."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / f"ti_cable_{int(cable_pf)}pf_{isolation_ohm:g}ohm.cir"
    deck.write_text(f"""Headgames TI LM358 isolated cable transient
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VIN plus 0 pulse(4.4 4.6 1m 1u 1u 4m 10m)
XU plus raw vcc 0 raw LMX58_LM2904
RISO raw cable {isolation_ohm:g}
CCABLE cable 0 {cable_pf:g}p
RLOAD cable vref 474k
VLOAD vref 0 4.5
.tran 2u 3m
.print tran v(cable)
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 2)
    require(rows, "TI cable transient produced no samples")
    post = [(time, voltage) for time, voltage in rows if time >= 1e-3]
    final_v = sum(voltage for _, voltage in post[-50:]) / min(50, len(post))
    step = final_v - 4.4
    require(step > 0.15, f"TI cable transient final step is only {step:.6g} V")
    overshoot = max(0.0, (max(voltage for _, voltage in post) - final_v) / step)
    band = abs(step) * 0.02
    settling = post[-1][0] - 1e-3
    for index, (time, voltage) in enumerate(post):
        if all(abs(later - final_v) <= band for _, later in post[index:]):
            settling = time - 1e-3
            break
    return overshoot, settling


def select_cable_isolation(values: dict[str, str]) -> tuple[float, float, float]:
    """Choose the smallest stocked resistor passing both TI cable loads."""
    stocked = sorted({item.value for item in read_inventory(BOM, values)
                      if item.kind == "R" and item.value >= 100.0})
    for resistor in stocked:
        results = tuple(spice_ti_cable_transient(cable, resistor)
                        for cable in (150.0, 250.0))
        if (max(result[0] for result in results) <= 0.20
                and max(result[1] for result in results) <= 1e-3):
            return resistor, max(result[0] for result in results), max(
                result[1] for result in results)
    raise VerificationError("no stocked cable-isolation resistor passes the TI model")


def spice_ti_rectifier_transient() -> tuple[float, float]:
    """Exercise the TI model in the precision rectifier's nonlinear loop."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / "ti_rectifier_tran.cir"
    deck.write_text(f"""Headgames TI LM358 precision-rectifier transient
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VREF vref 0 4.5
BINPUT alpha vref V=ternary_fcn(time>2 && time<3,0.2*sin(2*pi*10*(time-2)),0)
XRECT alpha hold vcc 0 drive LMX58_LM2904
D1 drive hold D4148
RDC drive hold 1g
RREL hold vref 220k
CHOLD hold vref 1u
.model D4148 D(Is=4n Rs=2 Cjo=4p N=1.9)
.ic v(hold)=4.5 v(drive)=4.5
.tran 500u 5.2 0 200u uic
.print tran v(hold)
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 2)
    require(rows, "TI rectifier transient produced no samples")
    baseline_values = [voltage-4.5 for time, voltage in rows if 1.5 <= time <= 1.9]
    require(baseline_values, "TI rectifier transient lacks a baseline")
    baseline = sum(baseline_values) / len(baseline_values)
    peak = max(voltage-4.5 for time, voltage in rows if 2.5 <= time <= 3.0)
    target = baseline + 0.1 * (peak-baseline)
    recovery = 2.0
    for time, voltage in rows:
        if time >= 3.0 and voltage-4.5 <= target:
            recovery = time-3.0
            break
    require(0.10 <= peak <= 0.30, f"TI rectifier held peak is {peak:.6g} V")
    require(recovery < 2.0, f"TI rectifier recovery is {recovery:.3f} s")
    return peak, recovery


def spice_filter_ac_crosscheck() -> tuple[float, float, float]:
    """Return nominal-model DC error and worst Python/SPICE AC errors."""
    require_spice_models()
    spice_ti_dc_characterization()
    dc_error = spice_nominal_model_dc_crosscheck()
    deck = generate_filter_spice_deck()
    completed = subprocess.run(["ngspice", "-b", str(deck)], cwd=deck.parent,
                               check=False, capture_output=True, text=True)
    require(completed.returncode == 0,
            f"ngspice failed: {(completed.stderr or completed.stdout)[-500:]}")
    points: list[tuple[float, float, float]] = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[0].isdigit():
            try:
                # ngspice 44 prints vp() in radians in batch tabular output.
                points.append((
                    float(fields[1]),
                    float(fields[2]),
                    math.degrees(float(fields[3])),
                ))
            except ValueError:
                continue
    require(points, "ngspice AC output contained no parseable points")
    parts, opamp = MfbStageParts(), OpAmpModel()
    worst_db = worst_phase = 0.0
    for target in (2.0, 8.0, 10.0, 12.0, 30.0, 60.0):
        frequency, magnitude, phase = min(points, key=lambda point: abs(point[0] - target))
        python_value = solve_cascade_ac(parts, parts, opamp, frequency).transfer
        worst_db = max(worst_db, abs(20 * math.log10(magnitude / abs(python_value))))
        phase_error = (phase - math.degrees(math.atan2(python_value.imag, python_value.real)) + 180) % 360 - 180
        worst_phase = max(worst_phase, abs(phase_error))
    require(worst_db <= 0.1, f"Python/SPICE AC magnitude differs by {worst_db:.3f} dB")
    require(worst_phase <= 1.0, f"Python/SPICE AC phase differs by {worst_phase:.3f} degrees")
    return dc_error, worst_db, worst_phase


