"""System-level QC check handlers — USB, I2C, RTC, GSM, and GSM signal.

Ported from the archived terminal-era QC package's check mixin: its
``_check_*`` methods become module-level functions that take a
:class:`QC.context.TestContext` instead of ``self``, and each function
self-registers under its bare catalog key via the ``qc_test`` decorator
(see :mod:`QC.catalog`). Handler bodies (subprocess calls, dmesg parsing,
i2cdetect logic) are unchanged from the old mixin — only host lookups are
rebound onto ``ctx``: ``self.log`` -> ``ctx.log``, ``self.ask_operator`` ->
``ctx.ask``, ``self.device_type``/``self.mac`` -> ``ctx.device_type``/
``ctx.mac``, and the ``CHECK`` context constant now comes from
:mod:`QC.logger` instead of the archive's registry module.

The archived package is never imported by this module (new code must not
depend on it). ``NM_STATUS_API`` and the ``_bridge_url`` helper that builds
on it now have a proper new-package home in :mod:`QC.network` (the
network-manager bridge constants and URL resolver belong with the net
handlers that use them most) and are imported from there rather than kept
as a local copy. ``I2C_ADDRESS_REGISTRY`` stays here, copied verbatim from
the archive's registry data, since it is specific to this module's I2C
scan check; the ``"system"`` sensor config already has a new-package home
in ``QC.catalog.SENSORS``.

``run_check_command``, the old shared dispatcher that resolved a handler by
name, invoked it, and recorded the result, is not ported as a separate
function — there is no longer a registry lookup step, since each check
function IS its own registered handler. Its post-processing (popping
``error``/``result`` out of the returned value dict, resolving the display
value, calling the record sink, and logging a PASS/FAIL audit line) is
preserved verbatim in the :func:`_finish` helper, shared by all five checks.
"""

import re
import subprocess
from importlib import import_module

import requests
from QC.catalog import SENSORS, qc_test
from QC.logger import CHECK
from QC.network import NM_STATUS_API, _bridge_url

# ═══════════════════════════════════════════════════════════════════════
#  Registry constants with no new-package home yet.
#  Copied verbatim from the archive's registry data — not imported from it.
# ═══════════════════════════════════════════════════════════════════════
I2C_ADDRESS_REGISTRY: dict[str, dict] = {
    "0x76": {
        "default": "BME280 (Temp/Humidity/Pressure)",
        "label": "OZTEMP_HUM_PRES_2",
    },
    "0x77": {
        "default": "BME280 (Temp/Humidity/Pressure)",
        "label": "OZTEMP_HUM_PRES_2",
    },
    "0x44": {
        "default": "SHT31 (Temp/Humidity)",
        "label": "OZTEMP_HUM_3",
    },
    "0x60": {
        # Channel-aware mapping when bus 0 is scanned via I2C mux GPIO selection.
        # Two maps, because the same two parts sit on different channels under
        # each selector: "mux" is the 4-position AB-pin layout, "mux_pca" the
        # 8-channel PCA9547 one. There is deliberately no fallback between
        # them — applying AB channel numbers to a PCA scan would confidently
        # name the wrong sensor, which is worse than naming none.
        "mux": {
            "1": {"device": "SI1147 (UV/Visible/IR)", "label": "OZUV_1"},
            "2": {"device": "MPL1152 (Pressure)", "label": "OZTEMP_HUM_PRES_4"},
        },
        "mux_pca": {
            "3": {"device": "MPL1152 (Pressure)", "label": "OZTEMP_HUM_PRES_4"},
            "6": {"device": "SI1147 (UV/Visible/IR)", "label": "OZUV_1"},
        },
        "default": "0x60 (Channel-dependent device)",
    },
    "0x38": {
        "default": "AHT20 (Temp/Humidity)",
    },
    "0x29": {
        "default": "TSL2591 (Light/Full Spectrum/IR)",
        "label": "OZUV_1",
    },
    "0x55": {
        "default": "SI1133 (UV/Light)",
    },
    "0x53": {
        "default": "LTR390 (Lux/UV/Radiation/Full Spectrum)",
    },
    "0x03": {
        "default": "AS3935 (Lightning Detector)",
        "label": "OZLN_1",
    },
    "0x36": {
        "default": "MAX17261 (Battery Fuel Gauge)",
    },
    "0x41": {
        "default": "ATHP Selector",
    },
    "0x0f": {
        "default": "SEMEATECH OGS (Gas Sensor)",
    },
    "0x31": {
        "default": "ELT CO₂ (CO₂)",
        "label": "OZCO2_1",
    },
    "0x61": {
        "default": "4-20mA Current Interface",
    },
    "0x62": {
        "default": "Sensirion SCD40 (CO₂/Temp/Humidity)",
        "label": "OZCO2_2",
    },
    "0x21": {
        "default": "MCP23017 (GPIO expanders)",
    },
    "0x70": {
        "default": "PCA9547 / TCA9548A (I2C multiplexer)",
    },
}

# The PCA9547 sits upstream of its own switch, so it answers in every channel
# scan. It is the selector, not a device at a position.
PCA9547_ADDRESS = "0x70"


def _resolve_i2c_label(
    entry: dict, bus: int, mux_channel: int | None, *, use_pca: bool = False
) -> tuple[str, bool]:
    """Resolve a human-readable device name for a detected I2C address.

    Applies the registry entry's rules in a fixed priority order: mux-channel
    rule first, then bus rule, then explicit ``label``, then ``default``
    device name, falling back to ``"Unknown / Not mapped"`` when nothing
    matches. Only an explicit label (from a mux entry or the top-level
    ``label`` key) marks the address as a mapped sensor; bus rules, mux
    ``device`` fallbacks, and defaults are treated as internal/unlabeled.

    Args:
        entry (dict): Entry from ``I2C_ADDRESS_REGISTRY`` for the address,
            optionally containing ``mux`` and ``mux_pca`` (dicts keyed by
            channel number as str), ``bus`` (dict keyed by bus number as
            str), ``label`` (str), and ``default`` (str).
        bus (int): I2C bus number on which the address was detected.
        mux_channel (int | None): Active mux channel during the scan, or
            ``None`` when the bus was scanned directly.
        use_pca (bool): ``True`` to read channel rules from the entry's
            ``mux_pca`` map (8-channel PCA9547 layout) instead of ``mux``
            (4-position AB-pin layout). The two maps are not interchangeable
            and never fall back to one another. Defaults to ``False``.

    Returns:
        tuple[str, bool]: ``(name, has_label)`` where ``name`` is the resolved
        device name and ``has_label`` is ``True`` only when an explicit label
        supplied the name.
    """
    if mux_channel is not None:
        mux_map = entry.get("mux_pca" if use_pca else "mux", {})
        if isinstance(mux_map, dict):
            mux_entry = mux_map.get(str(mux_channel))
            if isinstance(mux_entry, dict):
                label = mux_entry.get("label", "")
                if isinstance(label, str) and label and label != "unknown":
                    return label, True
                device = mux_entry.get("device", "")
                if isinstance(device, str) and device:
                    return device, False
            elif isinstance(mux_entry, str) and mux_entry:
                return mux_entry, False

    bus_map = entry.get("bus", {})
    if isinstance(bus_map, dict):
        bus_entry = bus_map.get(str(bus))
        if isinstance(bus_entry, str) and bus_entry:
            return bus_entry, False

    label = entry.get("label", "")
    if isinstance(label, str) and label:
        return label, True

    default = entry.get("default", "")
    if isinstance(default, str) and default:
        return default, False

    return "Unknown / Not mapped", False


def _get_wrapper(wrapper_name: str) -> object:
    """Import and instantiate the named OzWrapper driver class.

    Resolves ``OzWrapper.<wrapper_name>.<wrapper_name>`` by convention —
    package, module and class all share the wrapper name — and returns a
    fresh instance. Ported from the old QC runner's host-provided helper of
    the same name (there is no ``ctx`` equivalent — it is stateless).

    Args:
        wrapper_name (str): Wrapper package/class name, e.g. ``"OzSystem"``.

    Returns:
        object: New instance of the requested wrapper class.

    Raises:
        ModuleNotFoundError: If the wrapper package or module does not
            exist.
        AttributeError: If the module does not define a class named
            ``wrapper_name``.
        Exception: Whatever the wrapper's own ``__init__`` raises,
            typically on hardware or bus initialisation failure.
    """
    module = import_module(f"OzWrapper.{wrapper_name}.{wrapper_name}")
    return getattr(module, wrapper_name)()


def _finish(ctx, cmd: str, expected_value: str, result: tuple) -> bool:
    """Apply the result plumbing the old ``run_check_command`` dispatcher owned.

    Unpacks a handler's ``(passed, value)`` or ``(passed, value,
    display_value)`` return shape (exactly as the ported ``check_*`` bodies
    still produce), pops ``error``/``result`` out of ``value``, resolves the
    display value, records the outcome via ``ctx.record``, and logs a
    PASS/FAIL audit line — the same steps ``run_check_command`` performed
    for every check command, now shared by each self-registered function
    instead of a central dispatcher.

    Args:
        ctx (TestContext): Execution context providing the logger and the
            result sink.
        cmd (str): Catalog key the result is recorded under.
        expected_value (str): Human-readable expected outcome, recorded as
            the ``display_range``.
        result (tuple): ``(passed, value)`` or ``(passed, value,
            display_value)`` as computed by the calling check function.

    Returns:
        bool: The ``passed`` flag from ``result``, unchanged.
    """
    passed, value = result[0], result[1]
    display_value = result[2] if len(result) == 3 else ""
    error = value.pop("error", "")
    result_str = value.pop("result", "")
    if not display_value and result_str:
        display_value = result_str
    ctx.record(
        cmd,
        passed=passed,
        value=value,
        error=error,
        display_range=expected_value,
        display_value=display_value,
        kind="check",
    )
    if passed:
        ctx.log.info_with_context(CHECK, f"PASS  {cmd}  value={value}")
    else:
        ctx.log.error_with_context(CHECK, f"FAIL  {cmd}  value={value}")
    return passed


@qc_test("usb", kind="check", display="USB Check", expected="")
def check_usb(ctx) -> bool:
    """Enumerate USB devices with lsusb and record the parsed device list.

    Runs ``lsusb``, parses each output line into bus, device, ID, and
    description fields (lines that do not match the expected format are kept
    with ``"—"`` placeholder fields), and logs the parsed device list.

    Args:
        ctx (TestContext): Execution context providing the logger and the
            result sink.

    Returns:
        bool: ``True`` when at least one USB device is listed; ``False``
        when ``lsusb`` is missing, exits non-zero, the output is empty, or
        parsing raises.
    """
    cmd = "usb"
    expected_value = ""
    ctx.log.info_with_context(CHECK, f"CHECK  {cmd}")

    try:
        result = subprocess.run(["lsusb"], capture_output=True, text=True)
        if result.returncode != 0:
            err = (result.stderr or "").strip()
            ctx.log.error_with_context(CHECK, f"lsusb failed: {err}")
            return _finish(
                ctx, cmd, expected_value, (False, {"result": "USB error", "error": err})
            )

        devices: list[dict] = []
        pattern = re.compile(
            r"^Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s*(.*)$"
        )

        for line in result.stdout.splitlines():
            row = line.strip()
            if not row:
                continue
            match = pattern.match(row)
            if match:
                devices.append(
                    {
                        "bus": match.group(1),
                        "device": match.group(2),
                        "id": match.group(3),
                        "description": match.group(4) or "—",
                    }
                )
            else:
                devices.append(
                    {
                        "bus": "—",
                        "device": "—",
                        "id": "—",
                        "description": row,
                    }
                )

        if not devices:
            ctx.log.warning_with_context(CHECK, "No USB devices detected by lsusb")
            return _finish(
                ctx, cmd, expected_value, (False, {"result": "No USB devices detected"})
            )

        ctx.log.info_with_context(CHECK, f"USB devices: {devices}")
        ctx.log.info_with_context(CHECK, f"USB devices detected: {len(devices)}")
        detail = "\n".join(d["description"] for d in devices)
        return _finish(
            ctx,
            cmd,
            expected_value,
            (True, {"result": f"{len(devices)} USB device(s) detected\n{detail}"}),
        )
    except FileNotFoundError:
        msg = "lsusb command not found"
        ctx.log.error_with_context(CHECK, msg)
        return _finish(
            ctx, cmd, expected_value, (False, {"result": "USB error", "error": msg})
        )
    except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
        ctx.log.critical_with_context(CHECK, "USB check error")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "USB error", "error": str(exc)}),
        )


def _run_i2c_scan(ctx, cmd: str, *, use_pca: bool) -> bool:
    """Scan the board's I2C buses through one selector and map what answers.

    Shared body behind the two registered scans. ``use_pca`` picks which
    multiplexer the board is driven through, and that choice decides three
    things at once: how many channels exist on bus 0 (four AB-pin positions
    or eight PCA9547 channels), which channel->device map names the results
    (:func:`_resolve_i2c_label`), and — because it is written to the
    process-wide ``drivers.gpio.gpio.pca9547_flag`` — how **every later I2C
    sensor test in the same QC run** reaches its sensor. Selecting a scan is
    therefore selecting the bench's mux mode, not just running one check.

    The flag is set on both paths, never only on the PCA one, so an AB scan
    run after a PCA scan genuinely goes back to the AB pins instead of
    inheriting a mode from the earlier step.

    Bus 0 is walked channel by channel via ``gpio.select_I2C(ch, ch)``, then
    buses 6 and 10 are scanned directly, each with ``i2cdetect -y`` (bus 6
    is skipped on Helium boards, which have no MCP230XX).
    Addresses are parsed out of the i2cdetect grid, deduplicated, and
    resolved to device names through ``I2C_ADDRESS_REGISTRY``; the rows are
    sorted by bus, channel and address and logged as one summary. The
    recorded display value is a ``sensors:<n> internal-I2C:<m> total:<t>``
    line followed by one line per explicitly labeled sensor. Bus 10 output
    is additionally checked for a ``UU`` marker (the kernel having claimed
    the RTC) — logged only, it does not affect pass/fail. Per-bus failures
    are collected rather than aborting, so remaining buses are still
    scanned; any such failure fails the check.

    Args:
        ctx (TestContext): Execution context providing the logger and the
            result sink.
        cmd (str): Catalog key the result is recorded under — the caller's
            own registered key, so each scan reports as itself.
        use_pca (bool): ``True`` to drive the 8-channel PCA9547, ``False``
            for the legacy 4-position AB-pin selector.

    Returns:
        bool: ``True`` on a completed scan with no per-bus failures;
        ``False`` if the PCA9547 was requested but did not respond, if any
        mux select or bus scan failed, if ``i2cdetect`` is missing, or if
        the scan itself raises.

    Note:
        ``drivers.gpio.gpio`` is imported here, inside the function, rather
        than at module scope: importing it triggers ``RPi.GPIO.setmode()``
        at import time, which raises off a real Pi (and under the test
        stub). Keeping the import local means ``import QC.checks`` stays
        hardware-free.

        Mux channel selection mutates shared mux state; the last scanned
        channel remains selected after the check completes, and
        ``pca9547_flag`` stays where this scan left it for the rest of the
        process.
    """
    expected_value = ""
    ctx.log.info_with_context(CHECK, f"CHECK  {cmd}")

    # Keep scan plan hardcoded for this board to keep flow simple.
    mux_bus = 0
    mux_channels = list(range(8 if use_pca else 4))
    direct_buses = [10] if ctx.is_helium else [6, 10]
    selector = "PCA9547" if use_pca else "AB pin selector"

    scan_errors: list[dict] = []
    consolidated_rows: list[dict] = []
    channel_sets: dict[int, frozenset[str]] = {}
    total_detected = 0
    bus10_has_uu = False

    try:
        from drivers.gpio import gpio

        # Process-wide mode switch, read by select_I2C() on every later call.
        # Set before is_pca9547_available(), which returns False unless the
        # flag is already on regardless of what is on the bus.
        gpio.pca9547_flag = use_pca
        ctx.log.info_with_context(
            CHECK, f"I2C mux mode set to {selector} (pca9547_flag={use_pca})"
        )

        # Re-probe rather than trust a cached verdict. _ensure_mux() detects
        # once and remembers for the life of the process, so a mux that was
        # wedged or unpowered during an earlier step would keep reading as
        # absent after the operator reseats the cable — every re-run failing
        # until the QC app itself is restarted.
        if use_pca:
            gpio.reprobe_mux()

        if use_pca and not gpio.is_pca9547_available():
            # Without this guard the scan walks eight channels, finds nothing
            # on any of them, collects no scan_errors and reports PASS — a
            # missing mux would read on the bench as a board with no sensors.
            #
            # Put the flag back before returning. The operator running the whole
            # default list on an old board runs this scan too; leaving the mode
            # set to a mux that is not fitted would send every I2C sensor test
            # after it down the PCA path and fail them all with an unrelated
            # error. Reverting here is what makes running both scans safe on
            # either board: on the new PCB this one wins, on the old one it
            # undoes itself and the AB scan's mode stands.
            gpio.pca9547_flag = False
            msg = "PCA9547 did not respond on I2C — every channel would scan empty"
            ctx.log.error_with_context(CHECK, msg)
            return _finish(
                ctx,
                cmd,
                expected_value,
                (False, {"result": "PCA9547 not detected", "error": msg}),
            )

        targets: list[tuple[int, int | None]] = []
        for channel in mux_channels:
            targets.append((mux_bus, channel))
        for bus in direct_buses:
            targets.append((bus, None))

        for bus, mux_channel in targets:
            if mux_channel is not None:
                try:
                    # `ch` is the channel index on both backends (PCA9547
                    # channel 0-7, AB position 0-3 as raw A/B bits, #677); the
                    # pn arg is ignored whenever ch is set.
                    gpio.select_I2C(mux_channel, mux_channel)
                    ctx.log.info_with_context(
                        CHECK, f"Scanning I2C bus {bus} on mux channel {mux_channel}"
                    )
                except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
                    error = f"mux select failed: {exc}"
                    scan_errors.append(
                        {
                            "scan": f"Bus: {bus} Channel: {mux_channel}",
                            "error": error,
                        }
                    )
                    continue

            result = subprocess.run(
                ["i2cdetect", "-y", str(bus)], capture_output=True, text=True
            )
            if result.returncode != 0:
                error = (result.stderr or "").strip() or "scan failed"
                scan_errors.append(
                    {"scan": f"Bus: {bus} Channel: {mux_channel}", "error": error}
                )
                continue

            if bus == 10 and re.search(r"\buu\b", result.stdout.lower()):
                bus10_has_uu = True

            addresses: list[str] = []
            for line in result.stdout.splitlines():
                row_line = line.strip().lower()
                if not re.match(r"^[0-9a-f]{2}:", row_line):
                    continue
                for value in row_line.split()[1:]:
                    if re.match(r"^[0-9a-f]{2}$", value):
                        addresses.append(f"0x{value}")

            addresses = sorted(set(addresses), key=lambda item: int(item, 16))
            if use_pca and mux_channel is not None:
                # The PCA9547 sits upstream of its own switch, so it answers in
                # every channel scan. It is the selector, not a device at a
                # position — leaving it in shows the operator eight phantom
                # devices and inflates the total by eight.
                addresses = [item for item in addresses if item != PCA9547_ADDRESS]
            total_detected += len(addresses)
            if mux_channel is not None:
                channel_sets[mux_channel] = frozenset(addresses)

            for address in addresses:
                entry = I2C_ADDRESS_REGISTRY.get(address.lower(), {})
                device, has_label = _resolve_i2c_label(
                    entry, bus, mux_channel, use_pca=use_pca
                )
                consolidated_rows.append(
                    {
                        "bus": bus,
                        "mux_channel": mux_channel,
                        "address": address,
                        "device": device,
                        "has_label": has_label,
                    }
                )

        consolidated_rows = sorted(
            consolidated_rows,
            key=lambda item: (
                int(item.get("bus", 999)),
                99
                if item.get("mux_channel") is None
                else int(item.get("mux_channel", 99)),
                int(str(item.get("address", "0x00")), 16),
            ),
        )
        ctx.log.info_with_context(CHECK, f"I2C scan rows: {consolidated_rows}")

        if bus10_has_uu:
            ctx.log.info_with_context(CHECK, "Bus 10 RTC presence: UU detected")
        else:
            ctx.log.warning_with_context(CHECK, "Bus 10 RTC presence: no UU detected")

        # Every channel answering with the same non-empty set means the
        # selector never actually switched — on a board whose mux this
        # selector does not drive, each "channel" is just whatever the bus was
        # already on. The scan still completes and the table still looks
        # plausible, so without this the report carries four or eight copies
        # of one channel presented as distinct ones. Differing sets (including
        # populated vs empty) are normal and say nothing.
        unique_sets = set(channel_sets.values())
        mux_stuck = (
            len(channel_sets) > 1
            and len(unique_sets) == 1
            and bool(next(iter(unique_sets)))
        )
        if mux_stuck:
            ctx.log.warning_with_context(
                CHECK,
                "All %d channels returned identical devices — %s is not switching",
                len(channel_sets),
                selector,
            )

        sensor_labels = [row["device"] for row in consolidated_rows if row["has_label"]]
        internal_count = sum(1 for row in consolidated_rows if not row["has_label"])
        # State the total on the card as well. It is the same number the result
        # carries as total_detected, and without it on screen the operator sees
        # a larger figure in the log than either count shown here and reads it
        # as the internal count. sensors + internal == total by construction:
        # every consolidated row is one or the other.
        summary_line = f"{selector}  sensors:{len(sensor_labels)} internal-I2C:{internal_count} total:{len(consolidated_rows)}"
        display_value = summary_line + (
            "\n" + "\n".join(sensor_labels) if sensor_labels else ""
        )
        if mux_stuck:
            # First line, not a footnote: the rest of the card reads as a
            # normal result and the operator has no other way to tell a
            # working mux from one that is not being driven at all.
            display_value = (
                f"WARNING: all {len(channel_sets)} channels identical — {selector} not switching\n"
                + display_value
            )

        if scan_errors:
            ctx.log.error_with_context(CHECK, f"I2C scan errors: {scan_errors}")
            error_str = "; ".join(f"{e['scan']}: {e['error']}" for e in scan_errors)
            return _finish(
                ctx, cmd, expected_value, (False, {"error": error_str}, display_value)
            )

        ctx.log.info_with_context(
            CHECK,
            "I2C scan complete via %s for bus %s mux channels %s and buses %s total=%s",
            selector,
            mux_bus,
            mux_channels,
            direct_buses,
            total_detected,
        )
        return _finish(
            ctx,
            cmd,
            expected_value,
            (
                True,
                {"i2c": "ok", "selector": selector, "total_detected": total_detected},
                display_value,
            ),
        )
    except FileNotFoundError:
        msg = "i2cdetect command not found"
        ctx.log.error_with_context(CHECK, msg)
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "I2C scan error", "error": msg}),
        )
    except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
        ctx.log.critical_with_context(CHECK, "I2C scan check error")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "I2C scan error", "error": str(exc)}),
        )


@qc_test("i2c_scan_ab", kind="check", display="I2C Scan (AB pin selector)", expected="")
def check_i2c_scan_ab(ctx) -> bool:
    """Scan I2C through the legacy 4-position AB-pin mux (bus 0 channels 0-3).

    Clears ``drivers.gpio.gpio.pca9547_flag``, so every I2C sensor test run
    after this one in the same QC session is also reached through the AB
    address pins (BCM 24/25). Run this on boards without a PCA9547 fitted.

    Args:
        ctx (TestContext): Execution context providing the logger and the
            result sink.

    Returns:
        bool: ``True`` on a completed scan with no per-bus failures.
    """
    return _run_i2c_scan(ctx, "i2c_scan_ab", use_pca=False)


@qc_test("i2c_scan_pca", kind="check", display="I2C Scan (PCA9547)", expected="")
def check_i2c_scan_pca(ctx) -> bool:
    """Scan I2C through the 8-channel PCA9547 mux (bus 0 channels 0-7).

    Sets ``drivers.gpio.gpio.pca9547_flag``, so every I2C sensor test run
    after this one in the same QC session is also reached through the
    PCA9547 rather than the AB address pins. Fails immediately when no
    PCA9547 answers on the bus, rather than reporting eight empty channels
    as a clean pass. Run this on the new PCB.

    Args:
        ctx (TestContext): Execution context providing the logger and the
            result sink.

    Returns:
        bool: ``True`` on a completed scan with no per-bus failures;
        ``False`` when the PCA9547 is absent.
    """
    return _run_i2c_scan(ctx, "i2c_scan_pca", use_pca=True)


@qc_test("rtc", kind="check", display="RTC Check", expected="Synced")
def check_rtc(ctx) -> bool:
    """Check RTC health via dmesg and optionally sync it from system time.

    Greps ``dmesg`` for RTC messages. A ``"setting system clock"`` entry
    means the RTC synced the system clock at boot and the check passes
    immediately. Otherwise (power loss detected, or no conclusive entry) the
    operator is asked whether to sync the RTC from system time; on
    confirmation ``hwclock --systohc`` is run and the check passes only if
    it exits successfully.

    Args:
        ctx (TestContext): Execution context providing the logger, operator
            prompt, and the result sink.

    Returns:
        bool: ``True`` when the RTC is (or becomes) synced, ``False``
        otherwise.

    Note:
        Interactive — may block in ``ctx.ask()`` awaiting operator
        confirmation before syncing.
    """
    cmd = "rtc"
    expected_value = "Synced"
    ctx.log.info_with_context(CHECK, f"CHECK  {cmd}")

    try:
        result = subprocess.run(
            "dmesg | grep rtc", shell=True, capture_output=True, text=True
        )
        out = result.stdout
        ctx.log.debug_with_context(CHECK, f"RTC dmesg: {out.strip()}")

        if "setting system clock" in out:
            ctx.log.info_with_context(CHECK, "RTC: clock sync detected — working")
            return _finish(ctx, cmd, expected_value, (True, {"result": "Synced"}))

        if "Power loss detected" in out:
            ctx.log.error_with_context(CHECK, "RTC: power loss detected")
        else:
            ctx.log.warning_with_context(CHECK, "RTC: no conclusive dmesg entry")

        # NEXT STEP: ➡️ If RTC is not synced, ask operator before syncing
        confirmed = ctx.ask("RTC is not synced. Sync RTC now from system time?")
        if confirmed != "yes":
            ctx.log.warning_with_context(CHECK, "RTC sync declined by user")
            return _finish(ctx, cmd, expected_value, (False, {"result": "Not Synced"}))

        sync_result = subprocess.run(
            ["hwclock", "--systohc"], capture_output=True, text=True
        )
        if sync_result.returncode != 0:
            sync_err = (sync_result.stderr or "").strip()
            ctx.log.error_with_context(CHECK, f"RTC sync failed: {sync_err}")
            return _finish(ctx, cmd, expected_value, (False, {"result": "Not Synced"}))

        ctx.log.info_with_context(CHECK, "RTC synced via hwclock --systohc")
        return _finish(ctx, cmd, expected_value, (True, {"result": "Synced"}))
    except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
        ctx.log.critical_with_context(CHECK, "RTC check error")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "Not Synced", "error": str(exc)}),
        )


@qc_test("gsm", kind="check", display="GSM Check", expected="Internet Available")
def check_gsm(ctx) -> bool:
    """Check GSM internet connectivity via the network-manager bridge API.

    Queries the bridge's ``NM_STATUS_API`` endpoint with a 6-second timeout
    and reads the ``internet`` flag from the ``gsm`` section of the JSON
    response. The full GSM detail dict is logged.

    Args:
        ctx (TestContext): Execution context providing the logger and the
            result sink.

    Returns:
        bool: ``True`` when the bridge reports GSM internet access,
        ``False`` otherwise or on request/parse failure.
    """
    cmd = "gsm"
    expected_value = "Internet Available"
    ctx.log.info_with_context(CHECK, f"CHECK  {cmd}")

    try:
        # FIRST STEP: ➡️ Make API call to GSM status endpoint on the bridge and interpret results
        r = requests.get(_bridge_url() + NM_STATUS_API, timeout=6)
        gsm = r.json().get("gsm", {})
        internet = gsm.get("internet", False)
        ctx.log.info_with_context(CHECK, f"GSM status: {gsm}")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (
                bool(internet),
                {
                    "result": "Internet Available"
                    if internet
                    else "Internet Not Available"
                },
            ),
        )
    except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
        ctx.log.critical_with_context(CHECK, "GSM check error")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "Internet Not Available", "error": str(exc)}),
        )


@qc_test(
    "gsm_signal", kind="check", display="GSM Signal Check", expected="Signal Detected"
)
def check_gsm_signal(ctx) -> bool:
    """Report GSM signal strength and CPU temperature via the OzSystem wrapper.

    Instantiates the ``OzSystem`` wrapper through :func:`_get_wrapper`, initializes
    it from ``QC.catalog.SENSORS["system"]["config"]``, takes a reading with
    ``getSensorReading()``, then collects the final values with
    ``putSensorValue({})``. The returned dict carries CPU temperature under
    ``s1`` and GSM signal strength under ``sg``; it is logged in full and
    recorded as the check value via :func:`_finish` under the ``"gsm_signal"``
    catalog key. Any exception is caught and reported as a failed check rather
    than propagating, matching the ported handler contract shared by every
    check in this module.

    Args:
        ctx (TestContext): Execution context providing the logger and the
            result sink.

    Returns:
        bool: ``True`` when ``putSensorValue()`` returns a non-empty dict;
        ``False`` when it returns an empty dict or when wrapper import,
        initialization, reading, or value collection raises.

    Note:
        ``OzSystem.initialize`` requires a third ``mcpFlag`` argument (no
        default); the old handler's two-argument call raised TypeError on
        every run, so this check never worked on masterV2. QC passes
        ``mcpFlag=False``: the MCP230XX fan expander is irrelevant to
        reading metrics, and constructing it on boards without the chip
        raises outside the wrapper's own try block.
    """
    cmd = "gsm_signal"
    expected_value = "Signal Detected"
    ctx.log.info_with_context(CHECK, f"CHECK  {cmd}")

    try:
        # NEXT STEP: ➡️ If GSM check passed, we can also report signal strength info from the same API response
        sensor_obj = _get_wrapper("OzSystem")
        sensor_obj.initialize(SENSORS["system"]["config"], {}, mcpFlag=False)

        # NEXT STEP: ➡️ Take readings to allow sensor to stabilize and collect data
        sensor_obj.getSensorReading()

        # NEXT STEP: ➡️ Call putSensorValue to get the final value and record results
        value = sensor_obj.putSensorValue({})

        ctx.log.info_with_context(CHECK, f"System check: {value}")
        return _finish(ctx, cmd, expected_value, (bool(value), value))
    except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
        ctx.log.critical_with_context(CHECK, "GSM signal check error")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "Signal Not Available", "error": str(exc)}),
        )
