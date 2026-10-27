"""Actuator QC handlers — LED, fan, siren, display, relay, beacon, 4-20mA.

Ported from the archived terminal-era QC package's actuator mixin (the
legacy ``ActQC`` mixin): its ``_act_*`` methods become module-level functions
that take a :class:`QC.context.TestContext` instead of ``self``, and each
function self-registers under its bare catalog key via the ``qc_test``
decorator (see :mod:`QC.catalog`). Handler bodies (hardware exercising,
operator confirmation) are unchanged from the old mixin — only host lookups
are rebound onto ``ctx``: ``self.log`` -> ``ctx.log``, ``self.ask_operator``
-> ``ctx.ask``, ``self.device_type`` -> ``ctx.device_type``, and the ``ACT``
context constant now comes from :mod:`QC.logger` instead of the archive's
registry module.

The archived package is never imported by this module (new code must not
depend on it). The legacy ``ActQC`` mixin imported its hardware drivers
(``Adafruit_MCP4725``, ``drivers.gpio``, ``drivers.MCP230XX``,
``drivers.TM1637``, ``drivers.TM1638``, ``OzWrapper.OzRGB.OzRGB``) at module
scope; several of those raise off real hardware at import time (GPIO mode
setup, NeoPixel bus init). Every one of those imports is moved into the
function body (or ``try`` block) of whichever handler actually uses it, so
``import QC.actions`` stays hardware-free — matching the pattern
``QC.checks`` established for ``drivers.gpio`` in its I2C scan check.

``self.network_status`` / ``self.led_Thread`` — the two-slot queue and
daemon thread the old mixin cached on the QC runner instance to drive the
background OzRGB LED animation — have no instance to live on anymore, since
handlers are now free functions. They are kept as module-level globals
(``_network_status``, ``_led_thread``) instead, mutated by
:func:`_ensure_led_runtime`, whose own body is an unchanged port of
``ActQC._ensure_led_runtime``. Its call site now matches the legacy mixin
too: :class:`QC.session.QCSession`'s ``__init__`` calls it eagerly at
session construction, exactly as the legacy QC runner's ``__init__`` did.
:func:`act_led` also calls it on every invocation — an idempotent no-op
once the session has already started the runtime — as a safety net for
callers that exercise this module directly without going through
``QCSession`` (e.g. this module's own tests); see the note in
:func:`act_led`.

``run_act_command``, the old shared dispatcher that resolved a handler by
name, invoked it, and recorded the result, is not ported as a separate
function — there is no longer a registry lookup step, since each act
function IS its own registered handler. Its post-processing (popping
``error``/``result`` out of the returned value dict, resolving the display
value, calling the record sink, and logging a PASS/FAIL audit line) is
preserved verbatim in the :func:`_finish` helper, shared by all seven act
functions. Its pre-dispatch audit line (``"ACT  {cmd}  handler={name}"``) is
likewise preserved verbatim at the top of each function, with the handler
name hardcoded to what the old registry recorded for that command since
there is no longer a runtime name lookup to produce it.
"""

import os
import threading
import time
from queue import Queue

from QC.catalog import qc_test
from QC.logger import ACT


def mcp_device_number(default: int = 6) -> int:
    """Return the MCP23017 I2C device number from the ``MCP_ID`` environment variable.

    A systemd unit or ``docker -e MCP_ID`` passing an unset variable through
    yields ``MCP_ID=""``; ``int("")`` would raise inside the action's ``try`` and
    read as a hardware fault, so any unset, blank or non-numeric value falls
    back to ``default`` instead.

    Args:
        default (int): Device number used when ``MCP_ID`` is unset, blank or
            not an integer. Defaults to ``6``.

    Returns:
        int: The parsed device number, or ``default``.
    """
    try:
        return int(os.getenv("MCP_ID", "").strip())
    except ValueError:
        return default


# ═══════════════════════════════════════════════════════════════════════
#  LED RUNTIME — module-level state replacing the old mixin's instance
#  attributes (``self.network_status`` / ``self.led_Thread``).
# ═══════════════════════════════════════════════════════════════════════
_network_status: Queue | None = None
_led_thread: threading.Thread | None = None


def _ensure_led_runtime(ctx) -> None:
    """Lazily start the OzRGB LED runtime used by the LED actuator test.

    Creates the two-slot ``_network_status`` queue, spawns the daemon
    ``_led_thread`` running :func:`OzWrapper.OzRGB.OzRGB.main`, and switches
    the LED into QC mode (status index ``8``, blue breathing). Idempotent:
    if both module globals are already set it returns immediately without
    touching the hardware. Ported unchanged from ``ActQC._ensure_led_runtime``;
    only the state moved from instance attributes (``self.network_status``,
    ``self.led_Thread``) to the module globals ``_network_status`` and
    ``_led_thread``.

    Args:
        ctx (TestContext): Execution context providing the logger used to
            report initialization failures.

    Returns:
        None: The queue and thread are attached to the module globals as a
        side effect.

    Note:
        Initialization failures are logged as warnings and never raised, so a
        missing LED runtime degrades the LED test rather than aborting QC.
        Matching the legacy mixin's timing, :class:`QC.session.QCSession`'s
        ``__init__`` calls this once at session construction; :func:`act_led`
        also calls it on every invocation as an idempotent safety net (see
        the note there).
    """
    global _network_status, _led_thread
    if _network_status is not None and _led_thread is not None:
        return
    try:
        from OzWrapper.OzRGB.OzRGB import main as ozrgb_main

        _network_status = Queue(2)
        _network_status.put(0)
        _network_status.put(0)
        _led_thread = threading.Thread(
            target=ozrgb_main, args=(None, _network_status), daemon=True
        )
        _led_thread.start()
        _network_status.queue[0] = 8
    except Exception as exc:  # noqa: BLE001 — ported handler contract: degrade, never propagate
        ctx.log.warning_with_context(ACT, "LED runtime init failed: %s", exc)


def _finish(ctx, cmd: str, expected_value: str, result: tuple) -> bool:
    """Apply the result plumbing the old ``run_act_command`` dispatcher owned.

    Unpacks a handler's ``(passed, value)`` return shape (exactly as the
    ported ``act_*`` bodies still produce), pops ``error``/``result`` out of
    ``value``, records the outcome via ``ctx.record``, and logs a PASS/FAIL
    audit line — the same steps ``run_act_command`` performed for every
    actuator command, now shared by each self-registered function instead
    of a central dispatcher.

    Args:
        ctx (TestContext): Execution context providing the logger and the
            result sink.
        cmd (str): Catalog key the result is recorded under.
        expected_value (str): Human-readable expected outcome, recorded as
            the ``display_range``.
        result (tuple): ``(passed, value)`` as computed by the calling act
            function.

    Returns:
        bool: The ``passed`` flag from ``result``, unchanged.
    """
    passed, value = result[0], result[1]
    error = value.pop("error", "")
    display_value = value.pop("result", "")
    ctx.record(
        cmd,
        passed=passed,
        value=value,
        error=error,
        display_range=expected_value,
        display_value=display_value,
        kind="action",
    )
    if passed:
        ctx.log.info_with_context(ACT, f"PASS  {cmd}")
    else:
        ctx.log.error_with_context(ACT, f"FAIL  {cmd}")
    return passed


def _map_range(
    value: float, in_min: float, in_max: float, out_min: float, out_max: float
) -> float:
    """Linearly map a value from one numeric range onto another.

    Used by the 4-20 mA test to convert engineering values into DAC counts
    and DAC counts into expected loop current. Ported unchanged from
    ``ActQC._map_range`` (a ``@staticmethod`` there; a plain module function
    here, since there is no longer a class to hang it on).

    Args:
        value (float): Input value expressed in the input range.
        in_min (float): Lower bound of the input range.
        in_max (float): Upper bound of the input range; must differ from
            ``in_min``.
        out_min (float): Lower bound of the output range.
        out_max (float): Upper bound of the output range.

    Returns:
        float: ``value`` rescaled into the output range. Inputs outside the
        input range extrapolate linearly rather than clamp.

    Raises:
        ZeroDivisionError: If ``in_max`` equals ``in_min``.
    """
    return out_min + (((value - in_min) / (in_max - in_min)) * (out_max - out_min))


@qc_test(
    "led",
    kind="action",
    display="Indication LED Test",
    expected="Green, Cyan, Blue, Magenta, Yellow, Red, Purple, Neon Orange, Blue",
)
def act_led(ctx) -> bool:
    """Cycle the RGB status LED through all firmware states for operator review.

    Writes each of the nine status indices (0-8, from "Disconnected - Green
    blinking" through "QC MODE - Blue Breathing") into the
    ``_network_status`` queue for one second apiece so the OzRGB thread
    plays every colour and animation, then asks the operator to confirm the
    sequence.

    Args:
        ctx (TestContext): Execution context providing the logger, operator
            prompt, and the result sink.

    Returns:
        bool: ``True`` only if the operator confirmed the LED sequence,
        ``False`` otherwise or on failure.

    Note:
        Requires the LED runtime started by :func:`_ensure_led_runtime`;
        without it the write to ``_network_status`` raises and the test
        fails. :class:`QC.session.QCSession`'s ``__init__`` now calls
        ``_ensure_led_runtime()`` once at session construction, matching the
        legacy ``ActQC`` mixin's timing. The call here in ``act_led`` remains
        as an idempotent safety net — a cheap no-op once the runtime is
        already up — for callers that invoke this function without going
        through ``QCSession`` first (e.g. this module's own tests).
    """
    cmd = "led"
    expected_value = (
        "Green, Cyan, Blue, Magenta, Yellow, Red, Purple, Neon Orange, Blue"
    )
    ctx.log.info_with_context(ACT, f"ACT  {cmd}  handler=_act_led")

    try:
        # QC.session.QCSession.__init__ now calls _ensure_led_runtime() once
        # at session construction, matching the legacy mixin's timing. The
        # call here is kept as an idempotent safety net for callers that
        # invoke act_led without going through QCSession first (e.g. this
        # module's own tests) — a cheap no-op once the session has already
        # started the runtime.
        _ensure_led_runtime(ctx)
        led_status = (
            "Disconnected - Green blinking",
            "Connected - Cyan Breathing",
            "No Simcard - Blue Blinking",
            "Sending Data - Magenta Fast Blink",
            "Getting Config - Yellow Fast Blink",
            "Some Bug Found - Red Blink",
            "init success - Purple Blink",
            "OGS Allocation - Neon Orange Blinking",
            "QC MODE - Blue Breathing",
        )
        for i, status in enumerate(led_status):
            _network_status.queue[0] = i
            ctx.log.info_with_context(ACT, f"LED status {i}: {status}")
            time.sleep(1)

        # NEXT STEP: ➡️ Ask operator to confirm the LED states cycled correctly
        confirmed = ctx.ask("Did all LED states cycle correctly?")
        passed = confirmed == "yes"
        return _finish(
            ctx,
            cmd,
            expected_value,
            (
                passed,
                {
                    "result": "Green, Cyan, Blue, Magenta, Yellow, Red, Purple, Neon Orange, Blue"
                },
            ),
        )
    except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
        ctx.log.error_with_context(ACT, f"LED test error: {exc}")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "LED Error", "error": str(exc)}),
        )


@qc_test("fan", kind="action", display="Fan Test", expected="Fan Working")
def act_fan(ctx) -> bool:
    """Exercise the ventilation fan through the MCP230XX I/O expander.

    Resets the expander (device number taken from the ``MCP_ID`` environment
    variable, default ``6``) to its default state, drives the fan high,
    samples the fan feedback for five seconds via ``readFAN``, then switches
    the fan off. Unlike most actuator handlers this test is self-verifying
    from the feedback line and needs no operator confirmation.

    Args:
        ctx (TestContext): Execution context providing the logger and the
            result sink.

    Returns:
        bool: ``True`` when ``readFAN`` reports the fan running, ``False``
        otherwise or on failure.
    """
    cmd = "fan"
    expected_value = "Fan Working"
    ctx.log.info_with_context(ACT, f"ACT  {cmd}  handler=_act_fan")

    try:
        from drivers.MCP230XX import MCP230XX

        mcp = MCP230XX(devicenumber=mcp_device_number())
        mcp.resetDefault()

        # NEXT STEP: ➡️ Turn fan on, observe, then turn off
        mcp.FAN_HIGH()
        ctx.log.info_with_context(ACT, "Fan HIGH")
        fan_ok = mcp.readFAN(5)

        # NEXT STEP: ➡️ Turn fan off and confirm it stops
        mcp.FAN_LOW()
        ctx.log.info_with_context(ACT, f"Fan LOW — result: {fan_ok}")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (fan_ok, {"result": "Fan Working" if fan_ok else "Fan Not Working"}),
        )
    except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
        ctx.log.error_with_context(ACT, f"Fan test error: {exc}")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "Fan Error", "error": str(exc)}),
        )


@qc_test("siren", kind="action", display="Siren Test", expected="Buzzer Working")
def act_siren(ctx) -> bool:
    """Pulse the siren's LED and buzzer and ask the operator to confirm both.

    Selects the pin backend by board: Helium boards (``ctx.is_helium``) drive
    Raspberry Pi GPIO pins 22 (LED) and 23 (buzzer) directly, every other
    profile goes through MCP230XX expander pins 10 and 11 (expander number
    from the ``MCP_ID`` environment variable, default ``6``). Each part is
    switched on (one second on Helium, three seconds otherwise) and off
    for one second via the nested ``activate`` helper before the operator is
    asked whether both parts activated.

    Args:
        ctx (TestContext): Execution context providing the logger, the
            device type, the operator prompt, and the result sink.

    Returns:
        bool: ``True`` only if the operator confirmed both parts activated,
        ``False`` otherwise or on failure.
    """
    cmd = "siren"
    expected_value = "Buzzer Working"
    ctx.log.info_with_context(ACT, f"ACT  {cmd}  handler=_act_siren")

    try:
        if ctx.is_helium:
            from drivers.gpio import gpio

            parts = {"LED": 22, "Buzzer": 23}

            def activate(pin: int, on: bool) -> None:
                """Drive a single siren part pin to the requested logic level.

                Nested helper bound to the pin backend chosen in
                :func:`act_siren` — direct ``gpio`` calls on Helium
                devices, an ``MCP230XX`` expander otherwise. Configures the
                pin as an output on every call before writing the level.

                Args:
                    pin (int): Pin number of the siren part (LED or buzzer)
                        on the selected backend.
                    on (bool): True to energise the part, False to switch it
                        off.

                Returns:
                    None: The pin state is changed as a side effect.
                """
                gpio.setup(pin, gpio.OUT)
                gpio.set(pin, gpio.HIGH if on else gpio.LOW)
        else:
            from drivers.MCP230XX import MCP230XX

            mcp = MCP230XX(devicenumber=mcp_device_number())
            parts = {"LED": 10, "Buzzer": 11}

            def activate(pin: int, on: bool) -> None:
                """Drive a single siren part pin to the requested logic level.

                Nested helper bound to the pin backend chosen in
                :func:`act_siren` — direct ``gpio`` calls on Helium
                devices, an ``MCP230XX`` expander otherwise. Configures the
                pin as an output on every call before writing the level.

                Args:
                    pin (int): Pin number of the siren part (LED or buzzer)
                        on the selected backend.
                    on (bool): True to energise the part, False to switch it
                        off.

                Returns:
                    None: The pin state is changed as a side effect.
                """
                mcp.pinMode(pin, "output")
                mcp.digitalWrite(pin, 1 if on else 0)

        for name, pin in parts.items():
            # NEXT STEP: ➡️ Activate the siren part (LED or buzzer)
            activate(pin, True)
            ctx.log.info_with_context(ACT, f"Siren {name} ON  pin={pin}")
            time.sleep(3)

            # NEXT STEP: ➡️ Deactivate the siren part and confirm it stops
            activate(pin, False)
            ctx.log.info_with_context(ACT, f"Siren {name} OFF  pin={pin}")
            time.sleep(1)

        # NEXT STEP: ➡️ Ask operator to confirm both LED and buzzer activated
        confirmed = ctx.ask("Did LED and buzzer both activate?")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (
                confirmed == "yes",
                {
                    "result": "Buzzer Working"
                    if confirmed == "yes"
                    else "Buzzer Not Working"
                },
            ),
        )
    except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
        ctx.log.error_with_context(ACT, f"Siren test error: {exc}")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "Siren Error", "error": str(exc)}),
        )


@qc_test(
    "display",
    kind="action",
    display="7-segment Display Test",
    expected="pattern: 8.8.8.8.8.8.",
)
def act_display(ctx) -> bool:
    """Show a full-segment test pattern on the device display for operator review.

    On Helium boards (``ctx.is_helium``) a TM1638 lights its four discrete LEDs
    one by one, shows ``"8.8.8.8.8.8."`` for five seconds, and is then
    blanked, LEDs included. On all other devices four MCP230XX pins
    (12-15) are raised to enable the display, a TM1637 scrolls ``"888888"``,
    and both the display and the enable pins are cleared afterwards. The
    operator then confirms whether the pattern rendered correctly (every
    segment lit, so dead segments are visible).

    Args:
        ctx (TestContext): Execution context providing the logger, the
            device type, the operator prompt, and the result sink.

    Returns:
        bool: ``True`` only if the operator confirmed the pattern rendered
        correctly, ``False`` otherwise or on failure.
    """
    cmd = "display"
    expected_value = "pattern: 8.8.8.8.8.8."
    ctx.log.info_with_context(ACT, f"ACT  {cmd}  handler=_act_display")

    try:
        # FIRST STEP: ➡️ Activate the display with a test pattern
        if ctx.is_helium:
            from drivers.TM1638.TM1638 import TM1638

            display = TM1638(stb=3, clk=21, dio=20, brightness=7)
            display.clear_leds()
            # update_led only sets bits, so the LEDs build up A..D and stay lit
            # beside the digits: a dead one shows as a gap.
            for led in range(4):
                display.update_led(led)
                time.sleep(0.5)
            display.show("8.8.8.8.8.8.")
            time.sleep(5)
            display.write([0] * 6)
            time.sleep(1)
            display.clear_leds()
            question = "Did all 4 LEDs and the display show correctly?"
        else:
            from drivers.MCP230XX import MCP230XX
            from drivers.TM1637.TM1637 import TM1637Decimal

            mcp = MCP230XX(devicenumber=mcp_device_number())
            tm = TM1637Decimal(clk=21, dio=20)
            for pin in [12, 13, 14, 15]:
                mcp.pinMode(pin, "output")
                mcp.digitalWrite(pin, 1)
            tm.scroll("888888")
            tm.write([0] * 6)
            for pin in [12, 13, 14, 15]:
                mcp.digitalWrite(pin, 0)
            question = "Did the display show correctly?"

        # NEXT STEP: ➡️ Ask operator to confirm the display showed the pattern
        confirmed = ctx.ask(question)
        return _finish(
            ctx,
            cmd,
            expected_value,
            (
                confirmed == "yes",
                {
                    "result": "pattern: 8.8.8.8.8.8."
                    if confirmed == "yes"
                    else "Display Not Working"
                },
            ),
        )
    except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
        ctx.log.error_with_context(ACT, f"Display test error: {exc}")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "Display Error", "error": str(exc)}),
        )


@qc_test(
    "relay",
    kind="action",
    display="Relay Test",
    expected="Relay 1: Working, Relay 2: Working",
)
def act_relay(ctx) -> bool:
    """Click both output relays and ask the operator to verify each one.

    Toggles GPIO pins 26 (``output1``) and 16 (``output2``) high for one
    second then low, and asks the operator about each relay separately. The
    test passes only when both relays are confirmed working; partial
    failures are logged as warnings and the result string names the relay
    that failed.

    Args:
        ctx (TestContext): Execution context providing the logger, the
            operator prompt, and the result sink.

    Returns:
        bool: ``True`` only if both relays were confirmed working, ``False``
        otherwise or on failure.
    """
    cmd = "relay"
    expected_value = "Relay 1: Working, Relay 2: Working"
    ctx.log.info_with_context(ACT, f"ACT  {cmd}  handler=_act_relay")

    try:
        from drivers.gpio import gpio

        parts = {"output1": 26, "output2": 16}
        for name, pin in parts.items():
            # FIRST STEP: ➡️ Activate the relay
            gpio.setup(pin, gpio.OUT)
            gpio.set(pin, gpio.HIGH)
            ctx.log.info_with_context(ACT, f"Relay {name} ON  pin={pin}")
            time.sleep(1)

            # NEXT STEP: ➡️ Deactivate the relay confirm it clicks off
            gpio.set(pin, gpio.LOW)
            ctx.log.info_with_context(ACT, f"Relay {name} OFF  pin={pin}")
            time.sleep(1)

        # NEXT STEP: ➡️ Ask operator to confirm both relays clicked
        relay1 = ctx.ask("Did relay 1 click correctly?")
        relay2 = ctx.ask("Did relay 2 click correctly?")

        if relay1 == "yes" and relay2 == "yes":
            ctx.log.info_with_context(ACT, "Relays 1 and 2 both working")
            return _finish(
                ctx, cmd, expected_value, (True, {"result": "Both relays working"})
            )
        if relay1 == "yes":
            ctx.log.warning_with_context(ACT, "Relay 1 working, Relay 2 not working")
            return _finish(
                ctx,
                cmd,
                expected_value,
                (False, {"result": "Relay 1 working, Relay 2 not working"}),
            )
        if relay2 == "yes":
            ctx.log.warning_with_context(ACT, "Relay 2 working, Relay 1 not working")
            return _finish(
                ctx,
                cmd,
                expected_value,
                (False, {"result": "Relay 2 working, Relay 1 not working"}),
            )
        ctx.log.error_with_context(ACT, "Relays 1 and 2 both not working")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "Relays 1 and 2 both not working"}),
        )
    except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
        ctx.log.error_with_context(ACT, f"Relay test error: {exc}")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "Relay Error", "error": str(exc)}),
        )


@qc_test("beacon", kind="action", display="Beacon Test", expected="Beacon Working")
def act_beacon(ctx) -> bool:
    """Cycle the AQI beacon through its colour scale for operator review.

    Maps the test values 10, 50, 100, 200, 300 and 400 through
    :func:`OzWrapper.OzRGB.OzRGB.get_color_from_value`, floods the whole
    NeoPixel strip with each resulting RGB colour for two seconds, then asks
    the operator to confirm the colours cycled correctly.

    Args:
        ctx (TestContext): Execution context providing the logger, operator
            prompt, and the result sink.

    Returns:
        bool: ``True`` only if the operator confirmed the colours cycled
        correctly, ``False`` otherwise or on failure.
    """
    cmd = "beacon"
    expected_value = "Beacon Working"
    ctx.log.info_with_context(ACT, f"ACT  {cmd}  handler=_act_beacon")

    try:
        from OzWrapper.OzRGB.OzRGB import get_color_from_value, hex_to_rgb, pixels

        test_values = [10, 50, 100, 200, 300, 400]
        for val in test_values:
            # FIRST STEP: ➡️ Set beacon color based on test value and observe
            rgb = hex_to_rgb(get_color_from_value(val))
            pixels[:] = [rgb] * len(pixels)
            pixels.show()
            ctx.log.info_with_context(ACT, f"Beacon value={val} rgb={rgb}")
            time.sleep(2)

        # NEXT STEP: ➡️ Ask operator to confirm the beacon colors cycled
        confirmed = ctx.ask("Did beacon colors cycle correctly?")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (
                confirmed == "yes",
                {
                    "result": "Beacon Working"
                    if confirmed == "yes"
                    else "Beacon Not Working"
                },
            ),
        )
    except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
        ctx.log.error_with_context(ACT, f"Beacon test error: {exc}")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "Beacon Error", "error": str(exc)}),
        )


@qc_test(
    "420", kind="action", display="4-20mA Test", expected="range: 0-80, DAC: 700-3470"
)
def act_420(ctx) -> bool:
    """Sweep the 4-20 mA current-loop output across its full range.

    Selects I2C channel 0 via :func:`drivers.gpio.gpio.select_I2C`,
    initialises the MCP4725 DAC at address ``0x61`` on bus 0, and steps the
    engineering value 0-80 through :func:`_map_range` into DAC counts
    700-3470, holding each step for 0.2 seconds while logging the
    corresponding loop current (approximately 4.0-20.0 mA). The operator
    then confirms the loop responded.

    Args:
        ctx (TestContext): Execution context providing the logger, operator
            prompt, and the result sink.

    Returns:
        bool: ``True`` only if the operator confirmed the loop responded,
        ``False`` otherwise or on failure.

    Note:
        The sweep takes roughly 16 seconds (81 steps at 0.2 s each). The DAC
        is left at the final full-scale count after the sweep; no reset to
        zero is performed.
    """
    cmd = "420"
    expected_value = "range: 0-80, DAC: 700-3470"
    ctx.log.info_with_context(ACT, f"ACT  {cmd}  handler=_act_420")

    try:
        import Adafruit_MCP4725
        from drivers.gpio import gpio

        # FIRST STEP: ➡️ Select GPIO channel for 4-20mA output and initialize DAC
        # Part number 5, not 0: both land on AB position 0, but only 5 maps to
        # the PCA9547 channel the MCP4725 actually sits on (channel 2). With 0
        # this test silently talked to channel 0 on any board scanned through
        # the PCA9547 selector.
        gpio.select_I2C(5)
        dac = Adafruit_MCP4725.MCP4725(address=0x61, busnum=0)
        min_v, max_v, dacmin, dacmax = 0, 80, 700, 3470

        # NEXT STEP: ➡️ Loop through test values, set DAC output, and observe current loop response
        for val in range(81):
            send = _map_range(val, min_v, max_v, dacmin, dacmax)
            ma = _map_range(send, dacmin, dacmax, 40, 200) / 10
            dac.set_voltage(int(send))
            ctx.log.debug_with_context(
                ACT, f"4-20mA val={val} dac={int(send)} current={ma:.1f}mA"
            )
            time.sleep(0.2)

        # NEXT STEP: ➡️ Ask operator to confirm the 4-20mA loop responded
        confirmed = ctx.ask("Did the 4-20mA loop respond correctly?")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (
                confirmed == "yes",
                {"result": f"range_tested: 0-80 and dac_range: {dacmin}-{dacmax}"},
            ),
        )
    except Exception as exc:  # noqa: BLE001 — ported handler contract: report as failure, never propagate
        ctx.log.error_with_context(ACT, f"4-20mA test error: {exc}")
        return _finish(
            ctx,
            cmd,
            expected_value,
            (False, {"result": "4-20mA Error", "error": str(exc)}),
        )
