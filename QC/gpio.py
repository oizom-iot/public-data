"""GPIO abstraction for Raspberry Pi pin control.

This module provides a thin, simplified abstraction layer over the
:mod:`RPi.GPIO` library for use across Oizom hardware drivers. It exposes
helpers for pin configuration, digital read/write, cleanup, and selection
of channels on the on-board I2C multiplexer.

Callers may invoke :func:`read` or :func:`set` without an explicit prior
:func:`setup` call; the required direction is configured lazily on first use.
Deciding whether that is needed takes **two** checks, because two independent
states have to agree:

* ``_pin_modes``, a small in-process registry recording what this module has
  configured. :mod:`RPi.GPIO` keeps its own direction array that only
  ``setup()`` fills, and refuses ``output()``/``input()`` on a pin missing
  from it -- so a fresh process must re-``setup`` even a pad that is still
  physically an output from a previous run.
* ``GPIO.gpio_function``, which reads the SoC. Kernel drivers sharing these
  pins (the board runs an ``i2c-mux-pinctrl`` mux) reconfigure pads behind
  this module's back, and neither registry can see that happen.

Either one alone leaves a hole. The cache misses external reconfiguration;
the hardware read misses process-local state that was never established.

Example:
    Configure a pin and toggle its output level::

        >>> from drivers.gpio import gpio
        >>> gpio.setup(17, gpio.OUT)              # doctest: +SKIP
        >>> gpio.set(17, True)                     # doctest: +SKIP
        >>> gpio.read(4)                           # doctest: +SKIP
        True

Note:
    * The module initializes :mod:`RPi.GPIO` in **BCM** numbering mode
      (``GPIO.setmode(GPIO.BCM)``) at import time. Use :func:`setmode` to
      switch to ``GPIO.BOARD`` numbering if a different scheme is required,
      but be aware that all pin numbers throughout this module are assumed
      to be BCM.
    * Internal pull-up / pull-down resistors are **disabled by default**
      for inputs. Pass ``pullup=True`` or ``pullup=False`` to :func:`setup`
      to explicitly engage the SoC's internal pull resistor.
    * Runtime warnings emitted by :mod:`RPi.GPIO` are silenced at import
      time via ``GPIO.setwarnings(False)``; re-enable them with
      :func:`setwarnings` during debugging if needed.

See Also:
    :mod:`RPi.GPIO`
        Underlying low-level GPIO driver wrapped by this module.
"""

import os
import threading
import time

from RPi import GPIO
from utils.oizom_logger import OizomLogger

# -----------------------------------------------------------------------------
# Configure logging
# -----------------------------------------------------------------------------
context_logger = OizomLogger(__name__)

# Constants
IN: str = "in"
OUT: str = "out"
LOW: int = 0
HIGH: int = 1

# Set up default mode
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# Track mode per pin
_pin_modes = {}

# Mux state is populated lazily by _ensure_mux() on first select_I2C use, NOT
# at import time: constructing PCA9547()/MCP230XX() opens the SMBus and drives
# GPIO, so doing it at import made the module unimportable on dev machines, in
# unit tests, and in tooling (docs/linters that import it) and coupled load
# order to hardware state (#423). Consumers must call is_pca9547_available()
# rather than reading pca9547_available at import time (the value is False until
# the first probe runs).
pca9547 = None
mcp = None
pca9547_available = False
mcp_available = False
_mux_initialized = False
_mux_lock = threading.Lock()


def _ensure_mux() -> None:
    """Probe and initialise the PCA9547 mux and MCP230XX reset line once.

    Idempotent: the first call constructs the drivers (opening the SMBus and
    configuring the reset GPIO) and caches the result in module globals;
    subsequent calls return immediately. Called at the top of
    :func:`select_I2C` so the hardware is touched on first real use, never at
    import time.

    Order matters. The MCP is brought up *before* the PCA9547 is probed
    because MCP GPIO3 powers up high-Z (IODIR=0xFF) and PCA9547 RESET is
    active-LOW: a mux held in reset does not acknowledge on I2C, so probing
    first would make a fitted mux look absent and the reset line would never
    be driven.

    Args:
        None.

    Returns:
        None: Populates the ``pca9547``/``mcp``/``pca9547_available``/
        ``mcp_available`` globals in place.
    """
    global pca9547, mcp, pca9547_available, mcp_available, _mux_initialized
    # Double-checked lock: the fast path skips the lock once initialised; the
    # first callers serialize so the globals are fully populated before
    # _mux_initialized is set, and the drivers are constructed exactly once
    # even if two threads race the first select_I2C.
    if _mux_initialized:
        return
    with _mux_lock:
        if _mux_initialized:
            return
        try:
            from drivers.MCP230XX.MCP230XX import MCP230XX

            mcp = MCP230XX(devicenumber=int(os.getenv("MCP_ID", 6)))
            # GPIO3 powers up as a high-Z input (IODIR=0xFF), so digitalWrite would not
            # drive the RESET line. Configure it as a driven output, idle HIGH (RESET is
            # active-LOW), so a fitted mux is out of reset before we probe for it.
            mcp.pinMode(mcp.PCA9547, "output")
            mcp.digitalWrite(mcp.PCA9547, 1)
            mcp_available = True
        except Exception as e:
            context_logger.warning_with_context(
                "GPIO", f"MCP230XX not available for PCA9547 reset: {e}"
            )

        try:
            from drivers.PCA9547 import PCA9547

            pca9547 = PCA9547()
            pca9547_available = pca9547.is_connected()
        except Exception as e:
            context_logger.warning_with_context("GPIO", f"PCA9547 not available: {e}")

        # Set last, under the lock, so no concurrent caller sees the flag True
        # before the globals above are populated.
        _mux_initialized = True


def is_pca9547_available() -> bool:
    """Return whether a PCA9547 mux was detected, initialising on first call.

    Triggers the lazy hardware probe (:func:`_ensure_mux`) if it has not run
    yet, then reports the cached result. Prefer this over importing the
    ``pca9547_available`` global, which is a plain value bound at import and
    stays ``False`` for importers until the probe runs.

    Args:
        None.

    Returns:
        bool: ``True`` if the PCA9547 mux responded on the bus.
    """
    _ensure_mux()
    return pca9547_available


# How many reset-and-retry attempts before giving up on a wedged mux.
PCA9547_RESET_RETRIES = 3


def setup(
    pin: int, mode: str, pullup: bool | None = None, initial: bool = False
) -> None:
    """Configure a GPIO pin as input or output.

    Delegates to :func:`RPi.GPIO.setup` and records the chosen direction in the
    module-private ``_pin_modes`` registry, which :func:`set` and :func:`read`
    consult to decide whether this process has already configured the pin.

    Args:
        pin (int): BCM GPIO pin number to configure.
        mode (str): Direction to apply. Must be one of :data:`IN` (``"in"``)
            for input or :data:`OUT` (``"out"``) for output.
        pullup (bool | None): Optional pull resistor selection for inputs.
            ``True`` engages the internal pull-**up** resistor, ``False``
            engages the internal pull-**down** resistor, and ``None``
            (default) leaves the pin floating. Ignored when ``mode`` is
            ``"out"``.
        initial (bool): Initial output level applied when ``mode`` is
            ``"out"``. ``True`` drives the pin HIGH, ``False`` drives it LOW.
            Defaults to ``False`` (LOW). Ignored when ``mode`` is ``"in"``.

    Returns:
        None: Configures hardware state and updates ``_pin_modes`` in place.

    Raises:
        Exception: Nothing propagates to the caller. A ``mode`` other than
            ``"in"`` or ``"out"`` raises :class:`ValueError` internally, and
            that -- like any error from :mod:`RPi.GPIO` -- is caught and
            logged via the context logger. ``_pin_modes`` is left untouched
            when the call fails, so a later :func:`set` or :func:`read` will
            retry the configuration.

    Example:
        Configure pin 17 as an output initially driven LOW, and pin 4 as an
        input with the internal pull-up enabled::

            >>> setup(17, OUT, initial=False)      # doctest: +SKIP
            >>> setup(4, IN, pullup=True)          # doctest: +SKIP

    Note:
        Pin numbers are interpreted in **BCM** numbering. If :func:`setmode`
        has been used to switch to ``GPIO.BOARD`` numbering, callers must
        supply BOARD pin numbers instead.

    See Also:
        :func:`RPi.GPIO.setup`
    """
    try:
        if mode == IN:
            if pullup is not None:
                pud = GPIO.PUD_UP if pullup else GPIO.PUD_DOWN
                GPIO.setup(pin, GPIO.IN, pull_up_down=pud)
            else:
                GPIO.setup(pin, GPIO.IN)
        elif mode == OUT:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH if initial else GPIO.LOW)
        else:
            msg = f"Invalid mode: {mode}"
            raise ValueError(msg)
        _pin_modes[pin] = mode
    except Exception as e:
        context_logger.error_with_context(
            "GPIO", f"Failed to setup pin {pin} as {mode}: {e}"
        )


def set(pin: int, value: bool) -> None:
    """Drive a GPIO pin HIGH or LOW, auto-configuring it as output if needed.

    Before writing, two independent states must both agree that the pin is an
    output: the ``_pin_modes`` registry, recording what this process
    configured, and ``GPIO.gpio_function``, reading the SoC. If either
    disagrees, :func:`setup` is invoked first with ``mode=OUT``. Neither check
    alone is sufficient -- see the module docstring.

    Args:
        pin (int): BCM GPIO pin number to drive.
        value (bool): Logical level to apply. ``True`` drives the pin HIGH
            (3.3 V on the Pi), ``False`` drives it LOW (0 V).

    Returns:
        None: Performs a side effect on hardware and returns nothing. A
        failed write is logged but not reported, so callers that must know
        the line actually moved have to read it back.

    Raises:
        Exception: Any exception raised by :func:`setup` or :mod:`RPi.GPIO`
            is caught and logged via the context logger; nothing is
            re-raised.

    Example:
        Drive pin 18 HIGH::

            >>> set(18, True)                      # doctest: +SKIP

    Note:
        * This helper shadows the Python built-in ``set`` inside the module
          namespace. Importers may want to reach it through the module
          (``from drivers.gpio import gpio`` then ``gpio.set(...)``) to avoid
          the name clash.
        * The SoC check matters on this board: a kernel driver sharing the
          pin (the ``i2c-mux-pinctrl`` mux) can reclaim it and leave the pad
          as an input behind this module's back, and a write to an input pad
          is a silent no-op -- the mux would quietly stay on its previous
          channel.
        * Pin numbers must be BCM.

    See Also:
        :func:`output`
            Identically-behaving alias retained for API symmetry.
        :func:`RPi.GPIO.output`
    """
    try:
        # Two independent states must both say OUT, so both are checked.
        # _pin_modes tracks what this process configured: RPi.GPIO refuses
        # output() unless its own in-process direction array was filled by
        # setup(), and a fresh process inherits none of that even when the pad
        # is still physically an output from a previous run.
        # gpio_function() reads the SoC: a kernel driver can reclaim the pin and
        # leave FSEL as INPUT behind our back, and GPSET/GPCLR on an input pad is
        # a silent no-op, so the mux would quietly stay on the previous channel.
        if _pin_modes.get(pin) != OUT or GPIO.gpio_function(pin) != GPIO.OUT:
            setup(pin, OUT)
        GPIO.output(pin, GPIO.HIGH if value else GPIO.LOW)
    except Exception as e:
        context_logger.error_with_context(
            "GPIO", f"Failed to set pin {pin} to {value}: {e}"
        )


def read(pin: int) -> bool | None:
    """Read the current logic level of a GPIO pin.

    Before sampling, two independent states must both agree that the pin is an
    input: the ``_pin_modes`` registry, recording what this process
    configured, and ``GPIO.gpio_function``, reading the SoC. If either
    disagrees, :func:`setup` is invoked first with ``mode=IN`` and no pull
    resistor. Neither check alone is sufficient -- see the module docstring.

    Args:
        pin (int): BCM GPIO pin number to sample.

    Returns:
        bool | None: ``True`` when the pin is at logic HIGH, ``False`` when it
        is at logic LOW, and ``None`` when the read failed. Callers must
        distinguish ``None`` from ``False``: a bare truthiness test silently
        treats a failed read as a valid LOW.

    Raises:
        Exception: Any exception raised by :func:`setup` or :mod:`RPi.GPIO`
            is caught and logged via the context logger; nothing is
            re-raised. The failure surfaces as a ``None`` return value.

    Example:
        Sample an active-low button on pin 4, with the internal pull-up
        engaged by an earlier explicit :func:`setup` call::

            >>> pressed = read(4) is False         # doctest: +SKIP

    Note:
        * Because the lazy configuration path engages no pull resistor, a
          floating input will return non-deterministic values. Call
          :func:`setup` explicitly with the desired ``pullup`` argument
          before relying on :func:`read` for noisy or unconnected lines.
        * Pin numbers must be BCM.

    See Also:
        :func:`input`
            Identically-behaving alias retained for API symmetry.
        :func:`RPi.GPIO.input`
    """
    try:
        # Both states must say IN -- see the comment in set() for why one is not
        # enough. RPi.GPIO's input() likewise refuses a pin its own direction
        # array has never seen, however the pad is configured in silicon.
        if _pin_modes.get(pin) != IN or GPIO.gpio_function(pin) != GPIO.IN:
            setup(pin, IN)
        return bool(GPIO.input(pin))
    except Exception as e:
        context_logger.error_with_context("GPIO", f"Failed to read pin {pin}: {e}")
        return None


def input(pin: int) -> bool | None:
    """Read the logic level of a GPIO pin (alias for :func:`read`).

    This thin wrapper exists so that callers familiar with the
    :mod:`RPi.GPIO` API (``GPIO.input``) can use the same name when
    operating through this module.

    Args:
        pin (int): BCM GPIO pin number to sample.

    Returns:
        bool | None: ``True`` when the pin is at logic HIGH, ``False`` when
        it is at logic LOW, ``None`` when the read failed. Propagates the
        return value of :func:`read`.

    Raises:
        Exception: Any exception raised by :mod:`RPi.GPIO` is caught and
            logged inside :func:`read`; nothing is re-raised here.

    Example:
        Drop-in replacement for ``GPIO.input``::

            >>> level = input(17)                  # doctest: +SKIP

    Note:
        This function shadows the Python built-in :func:`input`. Inside
        modules that need both, alias the import (for example,
        ``from drivers.gpio import gpio`` and use ``gpio.input``).

    See Also:
        :func:`read`
        :func:`RPi.GPIO.input`
    """
    return read(pin)


def output(pin: int, value: bool) -> None:
    """Drive a GPIO pin HIGH or LOW (alias for :func:`set`).

    This thin wrapper exists so that callers familiar with the
    :mod:`RPi.GPIO` API (``GPIO.output``) can use the same name when
    operating through this module.

    Args:
        pin (int): BCM GPIO pin number to drive.
        value (bool): Logical level to apply. ``True`` drives the pin
            HIGH, ``False`` drives it LOW.

    Returns:
        None: This function delegates to :func:`set` and returns nothing.

    Raises:
        Exception: Any exception raised by :mod:`RPi.GPIO` is caught and
            logged inside :func:`set`; nothing is re-raised here.

    Example:
        Drop-in replacement for ``GPIO.output``::

            >>> output(17, True)                   # doctest: +SKIP

    Note:
        Auto-configures the pin as output via :func:`setup` if it has
        not been seen before. Pin numbers must be BCM.

    See Also:
        :func:`set`
        :func:`RPi.GPIO.output`
    """
    return set(pin, value)


def mode(pin: int) -> str | None:
    """Return the last-configured direction for a pin, if any.

    Looks up the pin in the module-private ``_pin_modes`` registry,
    which is populated by :func:`setup` (called either directly by the
    user or implicitly by :func:`set` / :func:`read`).

    Args:
        pin (int): BCM GPIO pin number to query.

    Returns:
        str | None: :data:`IN` (``"in"``) or :data:`OUT` (``"out"``) if
        the pin has been configured through this module, otherwise
        ``None``.

    Raises:
        None: This is a pure dictionary lookup and does not raise.

    Example:
        Inspect whether a pin has been configured yet::

            >>> mode(17)                           # doctest: +SKIP
            'out'
            >>> mode(99)                           # doctest: +SKIP

    Note:
        The registry is local to this module. Pins configured by other
        libraries acting on :mod:`RPi.GPIO` directly will not appear
        here even though they may be live on the SoC.
    """
    return _pin_modes.get(pin, None)


def cleanup(pin: int | None = None) -> None:
    """Release GPIO resources held by this process.

    Wraps :func:`RPi.GPIO.cleanup` and synchronizes the local
    ``_pin_modes`` registry by either clearing it entirely (when
    ``pin is None``) or removing a single entry.

    Args:
        pin (int | None): BCM GPIO pin number to release, or ``None`` to
            release **all** pins configured through this process.
            Defaults to ``None``.

    Returns:
        None: This function performs hardware cleanup and returns
        nothing.

    Raises:
        Exception: Any exception raised by :mod:`RPi.GPIO` is caught and
            logged as a warning via the context logger; nothing is
            re-raised.

    Example:
        Release a single pin or perform a global teardown at exit::

            >>> cleanup(17)                        # doctest: +SKIP
            >>> cleanup()                          # doctest: +SKIP

    Note:
        Calling ``cleanup()`` with no arguments resets every pin
        previously configured in **this process** to a safe input
        state. It does not affect pins held by other processes. It is
        good practice to invoke this from an ``atexit`` handler or
        signal handler in long-running applications.

    See Also:
        :func:`RPi.GPIO.cleanup`
    """
    try:
        if pin is None:
            GPIO.cleanup()
            _pin_modes.clear()
        else:
            GPIO.cleanup(pin)
            _pin_modes.pop(pin, None)
    except Exception as e:
        context_logger.warning_with_context("GPIO", f"Failed to cleanup pin {pin}: {e}")


def setwarnings(value: bool) -> None:
    """Enable or disable runtime warnings emitted by :mod:`RPi.GPIO`.

    Forwards directly to :func:`RPi.GPIO.setwarnings`. When warnings
    are enabled, :mod:`RPi.GPIO` prints a message if a pin is being
    configured that was already in use by another process or was not
    properly cleaned up previously.

    Args:
        value (bool): ``True`` to enable warnings, ``False`` to suppress
            them.

    Returns:
        None: This function configures the underlying library and
        returns nothing.

    Raises:
        None: This is a thin pass-through and does not raise on its own.

    Example:
        Re-enable warnings temporarily for debugging::

            >>> setwarnings(True)                  # doctest: +SKIP

    Note:
        At import time this module calls ``GPIO.setwarnings(False)`` to
        keep production logs clean. Toggling this back to ``True`` is
        typically only useful during development.

    See Also:
        :func:`RPi.GPIO.setwarnings`
    """
    GPIO.setwarnings(value)


def setmode(value: int) -> None:
    """Select the pin numbering scheme used by :mod:`RPi.GPIO`.

    The two supported schemes are:

    * ``GPIO.BCM`` -- Broadcom SoC channel numbers (default for this
      module). Pin numbers match the chip-level GPIO names.
    * ``GPIO.BOARD`` -- physical header pin numbers (1-40 on a
      standard 40-pin Pi header).

    Args:
        value (int): One of the numbering-mode constants exposed by
            :mod:`RPi.GPIO` -- ``GPIO.BCM`` or ``GPIO.BOARD``.

    Returns:
        None: This function configures the underlying library and
        returns nothing.

    Raises:
        ValueError: Raised by :mod:`RPi.GPIO` if the mode is invalid or
            conflicts with a previously-set mode in the same process.

    Example:
        Switch the process to BOARD numbering before configuring pins::

            >>> import RPi.GPIO as GPIO            # doctest: +SKIP
            >>> setmode(GPIO.BOARD)                # doctest: +SKIP

    Note:
        All other helpers in this module assume **BCM** numbering. If
        you call this function with ``GPIO.BOARD`` you must use header
        pin numbers consistently for every subsequent call. Switching
        modes mid-program is not supported by :mod:`RPi.GPIO`.

    See Also:
        :func:`RPi.GPIO.setmode`
    """
    GPIO.setmode(value)


def select_I2C(pn: int = 24, ch: int = -1) -> None:
    """Select a channel on the Oizom I2C multiplexer.

    Dispatches to whichever mux the board carries, as determined by the
    lazy probe in :func:`_ensure_mux`: an 8-channel PCA9547 on I2C when one
    responds, otherwise the legacy 4-position mux whose two address-select
    lines are wired to **BCM GPIO 24 and 25**. A 100 ms settling delay
    follows a successful switch so that downstream :mod:`smbus`
    transactions see a stable bus.

    Args:
        pn (int): Sensor part-number used to look up the channel.
            Defaults to ``24``. Ignored when ``ch`` is not ``-1``.
        ch (int): Optional override, applied when set to a value other
            than ``-1``. Its meaning differs per backend -- a PCA9547
            channel ``0``-``7``, or a substitute part number on the legacy
            mux. See :func:`ab_pin_selector`. Defaults to ``-1``.

    Returns:
        None: This function performs side effects on the mux and returns
        nothing. A failed switch is logged but not reported to the caller,
        which will read whatever channel the bus was left on.

    Raises:
        Exception: Any exception raised by :mod:`RPi.GPIO` or by the
            inner calls to :func:`set` is caught and logged via the
            context logger; nothing is re-raised.

    Example:
        Switch the mux to channel 2 before reading sensor 25::

            >>> select_I2C(25)                     # doctest: +SKIP
            >>> select_I2C(ch=2)                   # doctest: +SKIP

    Note:
        * The 100 ms ``time.sleep(0.1)`` after switching is empirically
          tuned for the Oizom mux and matches what other drivers in
          this repository expect; do not shorten it without retesting
          the full sensor stack.
        * If ``pn`` does not match any known mapping and ``ch`` is left
          at its default, the mux is **not** changed and remains on its
          current channel; only the settling delay is incurred.
        * On the legacy mux, GPIO 24 and 25 are reserved and must not be
          re-used by other drivers.

    See Also:
        :func:`set`
        :func:`RPi.GPIO.output`
    """
    # Lazily probe the mux on first real use (never at import time, #423).
    # _ensure_mux()
    # if pca9547_available:
    #     pca9547_channel_selector(pn, ch)
    # else:
    ab_pin_selector(pn, ch)
    time.sleep(0.1)


def pca9547_channel_selector(pn: int = 24, ch: int = -1) -> None:
    """Select a PCA9547 mux channel, resetting the mux if it wedges.

    Maps a sensor part number -- or an explicit channel override -- onto one of
    the eight PCA9547 channels and switches the mux to it. The mux is then
    re-probed on I2C; if it no longer acknowledges, the channel is treated as
    wedged and the MCP230XX RESET line is pulsed up to
    :data:`PCA9547_RESET_RETRIES` times to recover it.

    The part-number to channel mapping is:

    * channel 0 -- ``0`` (also the parking channel used during recovery)
    * channel 2 -- ``5``
    * channel 3 -- ``25``, ``26``
    * channel 4 -- ``29``
    * channel 5 -- ``23``, ``24``, ``54``
    * channel 6 -- ``32``, ``35``, ``36``
    * channel 7 -- ``3``

    Args:
        pn (int): Sensor part number used to look up the channel. Defaults to
            ``24``. Ignored when ``ch`` is a valid channel index. A part
            number outside the mapping above is a no-op: the function returns
            without touching the mux, leaving it on its current channel.
        ch (int): Explicit channel override, applied when in the range ``0``
            to ``7`` inclusive. Any other value -- ``-1`` by default -- falls
            back to the ``pn`` mapping. Defaults to ``-1``.

    Returns:
        None: Switches the mux in place and returns nothing. Neither a wedged
        mux nor a failed recovery is reported to the caller; both are logged,
        and the caller's subsequent bus transaction is expected to fail.

    Raises:
        Exception: Any exception raised by the PCA9547 or MCP230XX drivers is
            caught and logged via the context logger; nothing is re-raised.

    Example:
        Select the channel for part number 26, then force channel 2
        directly::

            >>> pca9547_channel_selector(26)       # doctest: +SKIP
            >>> pca9547_channel_selector(ch=2)     # doctest: +SKIP

    Note:
        * Recovery deliberately parks the mux on **channel 0**, which is
          empty, instead of re-selecting the requested channel. Channel 0
          isolates the fault; re-exposing the bus to whatever downstream
          device wedged the mux would simply wedge it again. The caller's
          read then finds nothing and fails, which is the intended outcome
          and not a defect to be "fixed".
        * Without an MCP230XX (``mcp_available`` is ``False``) there is no
          RESET line to pulse, so a wedged mux is logged as an error and
          left as-is.
        * Callers should normally go through :func:`select_I2C`, which runs
          the lazy hardware probe and dispatches here only when a PCA9547
          actually responded on the bus.

    See Also:
        :func:`select_I2C`
        :func:`ab_pin_selector`
            Legacy fallback used when no PCA9547 is fitted.
        :func:`_ensure_mux`
    """
    try:
        # Map part number / explicit channel to a PCA9547 channel index.
        if 0 <= ch <= 7:
            channel = ch
        elif pn in [25, 26]:
            channel = 3
        elif pn in [5]:
            channel = 2
        elif pn in [0]:
            channel = 0
        elif pn in [29]:
            channel = 4
        elif pn in [23, 24, 54]:
            channel = 5
        elif pn in [32, 35, 36]:
            channel = 6
        elif pn in [3]:
            channel = 7
        else:
            return

        ok = pca9547.select_channel(channel)
        if ok and pca9547.is_connected():
            return

        if not mcp_available:
            context_logger.error_with_context(
                "GPIO", f"PCA9547 channel {channel} wedged and no MCP to reset"
            )
            return

        for attempt in range(1, PCA9547_RESET_RETRIES + 1):
            context_logger.warning_with_context(
                "GPIO",
                f"PCA9547 wedged on channel {channel}, reset {attempt}/{PCA9547_RESET_RETRIES}",
            )
            mcp.pca9547_reset_pulse()
            time.sleep(0.05)
            # Park on channel 0 deliberately -- it is empty, so it isolates the
            # fault. Re-selecting the requested channel would re-expose the bus
            # to whatever downstream device wedged it. The caller's read then
            # finds nothing and fails, which is the correct outcome.
            ok = pca9547.select_channel(0)
            if ok and pca9547.is_connected():
                context_logger.warning_with_context(
                    "GPIO", "PCA9547 recovered on channel 0"
                )
                return

        context_logger.error_with_context(
            "GPIO", f"PCA9547 still wedged on channel {channel}; parked at ch0"
        )
    except Exception as e:
        context_logger.error_with_context(
            "GPIO", f"Failed to select PCA9547 channel {pn}: {e}"
        )


def ab_pin_selector(pn: int = 24, ch: int = -1) -> None:
    """Select a position on the legacy 4-way I2C mux via GPIO 24 and 25.

    Fallback used by :func:`select_I2C` when no PCA9547 responds on the bus.
    The mux position is a 2-bit value driven onto the two address-select
    lines, **BCM GPIO 24** (low bit, ``A``) and **BCM GPIO 25** (high bit,
    ``B``). Part numbers are grouped so that every sensor sharing a physical
    branch drives the same pair of levels:

    * ``A=0, B=0`` -- ``0``, ``5``, ``18``, ``23``, ``24``, ``29``, ``53``,
      ``54``
    * ``A=1, B=0`` -- ``1``, ``32``, ``33``, ``34``, ``35``, ``36``, ``38``,
      ``39``
    * ``A=0, B=1`` -- ``2``, ``25``, ``26``, ``27``, ``28``
    * ``A=1, B=1`` -- ``3``, ``4``

    Args:
        pn (int): Sensor part number used to look up the mux position.
            Defaults to ``24``. Overwritten by ``ch`` when ``ch`` is not
            ``-1``. A part number outside the groups above leaves both select
            lines untouched, so the mux stays on its current position.
        ch (int): Substitute part number, applied when set to anything other
            than ``-1``. This is **not** a raw channel index -- it replaces
            ``pn`` and is resolved through the same lookup, so the value
            passed must itself be a part number appearing in the groups
            above. Defaults to ``-1``.

    Returns:
        None: Drives the two select lines and returns nothing. A failed
        switch is logged but not reported to the caller, which will then read
        whatever branch the mux was left on.

    Raises:
        Exception: Any exception raised by :func:`set` or :mod:`RPi.GPIO` is
            caught and logged via the context logger; nothing is re-raised.

    Example:
        Select the branch carrying part number 26::

            >>> ab_pin_selector(26)                # doctest: +SKIP
            >>> ab_pin_selector(ch=26)             # doctest: +SKIP

    Note:
        * GPIO 24 and 25 are reserved for this mux on boards without a
          PCA9547 and must not be re-used by other drivers.
        * The select lines are driven through :func:`set`, which
          auto-configures each pin as an output and re-checks the SoC
          direction on every call -- necessary because kernel drivers
          sharing these pads can reclaim them between switches.
        * No settling delay is applied here; :func:`select_I2C` sleeps 100 ms
          after dispatching, so callers driving this function directly must
          add their own delay before touching the bus.

    See Also:
        :func:`select_I2C`
        :func:`pca9547_channel_selector`
            Preferred path when a PCA9547 mux is fitted.
        :func:`set`
    """
    try:
        if ch != -1:
            pn = ch
        if pn in [0, 5, 18, 23, 24, 29, 53, 54]:
            set(24, 0)
            set(25, 0)
        elif pn in [1, 32, 33, 34, 35, 36, 38, 39]:
            set(24, 1)
            set(25, 0)
        elif pn in [2, 25, 26, 27, 28]:
            set(24, 0)
            set(25, 1)
        elif pn in [3, 4]:
            set(24, 1)
            set(25, 1)
    except Exception as e:
        context_logger.error_with_context(
            "GPIO", f"Failed to select I2C channel {pn}: {e}"
        )
