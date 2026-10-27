"""QC session engine — per-run config overrides, dispatch, and result recording.

Ported from the archived terminal-era QC package's session core (the legacy
``QCCore`` class in its ``core.py``): the session state, ``ask_operator``
delegation, ``holo_register`` device onboarding call, ``_record`` result
sink, and ``DEVICE_PROFILES`` table are carried over largely unchanged.
Terminal/LED-mode selection and the ad hoc ``importlib`` wrapper resolver
that used to live on the session class are NOT ported here — wrapper
resolution now belongs to the sensor pipeline (see
:func:`QC.sensor_test._load_wrapper`), and there is no terminal UI in this
package.

Per-run config overrides are full-replace: the web UI's JSON editor sends
the operator's complete edited config for a sensor, and :meth:`QCSession.execute`
uses it verbatim for that run instead of the default stored in
``QC.catalog.SENSORS`` — the shared default itself is never mutated.

The archived package is never imported by this module (new code must not
depend on it). ``QCSession`` is the new-package home for the HOLO
device-registration constants the old session core relied on
(``QC_VERSION``, ``OIZOM_MANAGER``, ``HOLO_API``, ``API_HEADERS``) — copied
verbatim from the archive's registry module.
"""

import copy
import json
import logging
import os
import pathlib
from datetime import datetime
from types import SimpleNamespace

import requests
from QC.actions import _ensure_led_runtime
from QC.catalog import CATALOG, SENSORS, TESTS
from QC.context import TestContext
from QC.logger import ALLOC, DEFAULT, HOLO, SESSION, build_qc_logger
from QC.registry_load import load_all
from QC.report_store import ReportStore
from QC.sensor_test import run_sensor

# ═══════════════════════════════════════════════════════════════════════
#  API / NETWORK CONSTANTS — new-package home.
#  Copied verbatim from the archive's registry module.
# ═══════════════════════════════════════════════════════════════════════
QC_VERSION: str = "2.0.0"
OIZOM_MANAGER = "http://manager.oizom.com"
HOLO_API = "/v2/qc/init/"
# Sensor allocation: binds a physical sensor's ID to its QC reading.
OIZOM_SOCKET = "http://socket.oizom.com"
QC_API = "/qc/sensor/data"
# Catalog key -> the terminal tool's command letter. The Node-RED flow behind
# QC_API (QC/flows.json, commandIdToCells) picks the Google Sheet tab from
# CommandId and only knows these letters; any other CommandId lands on the
# "Default" tab. Each pairing matches the Old_QC.py sensor with the same part
# number (pn). Sensors with no tab in the flow send their catalog key.
LEGACY_COMMAND_IDS: dict[str, str] = {
    "sht31": "d",  # SHT
    "bme280": "e",  # BME
    "max17261": "f",  # BATTERY
    "pm3006": "h",  # DUST (Cubic PM3006, pn 18)
    "ads122u04": "j",  # OGS (single position)
    "ogs_pos0": "j",
    "ogs_pos1": "j",
    "ogs_pos2": "j",
    "ogs_pos3": "j",
    "ogs_pos4": "j",
    "ogs_pos5": "j",
    "ogs_pos6": "j",
    "ogs_pos7": "j",
    "ogs": "sb",  # SB-v21 (all eight OGS positions, Old_QC allOGS)
    "tsl2591": "k",  # Light-UV35 (Old_QC lightuv = pn 32 + pn 35)
    "si1147": "k",
    "ltr390": "ag",  # UV-36
    "samdV2_noise": "n2",  # Noise
    "hongyu_wind": "o2",  # Wind (pn 211)
    "tipping_bucket": "p2",  # Rain
    "mpl1152": "ae",  # ATHP (Old_QC athp = SHT31 + MPL1152)
    "lps25hb": "ae2",  # SHT+LPS (Old_QC athp2 = SHT31 + LPS25HB)
    "scd40": "ad",  # CO2
    "aht20": "ht",  # AHT20
}
# Sensor pairs the terminal tool read as ONE sensor (Old_QC lightuv/athp/athp2).
# Registered together they get one Sensor ID, this CommandId, and one merged
# SensorData; each member can still be registered alone under its own
# LEGACY_COMMAND_IDS letter. Labels are the flow's Google Sheet tab names.
COMBINED_REGISTRATIONS: dict[str, dict] = {
    "k": {"label": "Light-UV35", "keys": ("tsl2591", "si1147")},
    "ae": {"label": "ATHP", "keys": ("sht31", "mpl1152")},
    "ae2": {"label": "SHT+LPS", "keys": ("sht31", "lps25hb")},
}

# Hardware revision recorded against every device registered from this bench.
# Sent verbatim as the HOLO payload's "note". Overridable per bench through
# OIZOM_HW_REV (like OIZOM_API_TOKEN below) so an old checkout does not stamp
# new-revision hardware with a stale constant; the default is the fallback.
HOLO_NOTE = os.environ.get("OIZOM_HW_REV", "V6.4")
# Token comes from the environment on QC benches; the in-repo fallback keeps
# existing benches working until OIZOM_API_TOKEN is rolled out everywhere.
_FALLBACK_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOjIwODksIm9yZ0lkIjoiT1pfQVBJUyIsInVzZXJFbWFpbCI6ImFwaXNAb2l6b20uY29tIiwiaWF0IjoxNjExNTU5MDE5LCJleHAiOjE4MzIzMTEwMTksImlzcyI6ImxiVzRvOUlUVTZTcHZXekRyeWV3TWVqTjF6cXM4NkVZIn0.8W9fVYBfWMZ931WlfEoL-dFIl9zdTNbMyb3A3dzjags"
API_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer " + os.getenv("OIZOM_API_TOKEN", _FALLBACK_TOKEN),
}


def token_source() -> str:
    """Describe where the HOLO bearer token in :data:`API_HEADERS` came from.

    Public accessor for diagnostics (``scripts/holo_probe.py``), so callers never
    need the private ``_FALLBACK_TOKEN`` name. Never returns the token itself.

    Returns:
        str: ``"OIZOM_API_TOKEN env var"`` when the variable was set at import
        time (even to an empty string), otherwise ``"in-repo fallback (2021)"``.
    """
    return (
        "OIZOM_API_TOKEN env var"
        if "OIZOM_API_TOKEN" in os.environ
        else "in-repo fallback (2021)"
    )


class HoloPrecondition(ValueError):
    """Raised by :meth:`QCSession.holo_register` when the request cannot be built.

    Distinguishes a local precondition failure (no Particle ID in the session, so
    no valid endpoint URL exists) from an upstream or network failure, which is
    reported through the ``(False, message)`` return value instead. The HTTP
    layer maps this to a 4xx, never a 5xx.
    """


# Device profiles are runtime-configurable session data, not static hardware
# data, so they live here instead of the catalog. Keys are the internal
# codes used in the QC web form; values are operator-facing product names.
DEVICE_PROFILES: dict[str, str] = {
    "PD": "Polludrone",
    "PS": "Pollusense",
    "AQ": "AQBot",
    "AQH": "AQH",
}


def _merge_samples(parts: dict[str, dict]) -> dict:
    """Merge each member sensor's readings into one combined SensorData dict.

    Args:
        parts (dict[str, dict]): Catalog key -> that sensor's readings, one
            list per parameter, in ``COMBINED_REGISTRATIONS`` member order,
            e.g. ``{"sht31": {"temp": [...], "hum": [...]},
            "lps25hb": {"temp": [...], "pr": [...]}}``.

    Returns:
        dict: The combined readings sent as SensorData. Members are merged
        with :meth:`dict.update` in member order, so when two members publish
        the same code the later member's readings win (sht31 and lps25hb both
        send ``temp`` — the ``ae2`` row carries lps25hb's; tsl2591 and si1147
        both send ``ir`` — the ``k`` row carries si1147's). This is what the
        terminal tool sent: its combined wrapper built the payload with
        ``value[sc] = ...`` in config order, so a repeated code was
        overwritten the same way.
    """
    merged: dict = {}
    for readings in parts.values():
        merged.update(readings)
    return merged


class _StdlibLoggerAdapter:
    """Adapt a plain :class:`logging.Logger` to the ``*_with_context`` surface.

    Used only when :func:`QC.logger.build_qc_logger` itself fails to
    construct the real :class:`utils.oizom_logger.OizomLogger` (e.g. a
    broken log directory). Every consumer of ``self.log``/``ctx.log``
    elsewhere in this package — :mod:`QC.checks`, :mod:`QC.actions`,
    :mod:`QC.network`, :mod:`QC.sensor_test`, and this module — assumes the
    ``*_with_context(context, message, *args, **kwargs)`` surface, so a bare
    stdlib logger cannot stand in for it directly; this thin wrapper folds
    the context token into the message text and forwards to the wrapped
    logger's matching plain method.

    Attributes:
        _logger (logging.Logger): The stdlib logger this adapter delegates
            to.
        logger (logging.Logger): Same wrapped stdlib logger, exposed under
            the name :class:`QC.runner.QCRunner` expects — its SSE log
            handler wiring does ``getattr(core.log, "logger", core.log)``
            then calls ``.addHandler(...)`` on the result, mirroring how it
            reaches into a real ``OizomLogger``. Without this attribute that
            ``getattr`` would fall back to the adapter itself, which has no
            ``addHandler`` method, so ``QCRunner.__init__`` would raise the
            moment a session fell back to this adapter — silently negating
            the stdlib-logger fallback.
    """

    def __init__(self, logger: logging.Logger) -> None:
        """Bind the stdlib logger this adapter delegates to.

        The same logger instance is stored twice on purpose: privately as
        ``_logger`` (the delegation target used by :meth:`_log`) and publicly as
        ``logger`` (the attribute name :class:`QC.runner.QCRunner`'s SSE handler
        wiring resolves via ``getattr(core.log, "logger", core.log)`` before
        calling ``.addHandler(...)``, mirroring a real
        ``utils.oizom_logger.OizomLogger``).

        Args:
            logger (logging.Logger): Stdlib logger instance to wrap.

        Returns:
            None.
        """
        self._logger = logger
        self.logger = logger

    def _log(self, level: str, context: str, message: str, *args, **kwargs) -> None:
        """Forward one record to the wrapped logger, prefixing the context token.

        Args:
            level (str): Name of the stdlib :class:`logging.Logger` method to
                call (``"debug"``, ``"info"``, ``"warning"``, ``"error"`` or
                ``"critical"``).
            context (str): Context token; folded into the message text since
                a plain :class:`logging.Logger` has no separate context
                column.
            message (str): ``%``-style format string.
            *args: Positional substitution arguments for ``message``.
            **kwargs: Forwarded to the underlying logger method.

        Returns:
            None.
        """
        getattr(self._logger, level)(f"[{context}] {message}", *args, **kwargs)

    def debug_with_context(self, context: str, message: str, *args, **kwargs) -> None:
        """Emit a DEBUG record carrying a context token.

        Thin convenience wrapper over :meth:`_log` with ``level="debug"``.

        Args:
            context (str): Context token; folded into the message text.
            message (str): ``%``-style format string.
            *args: Positional substitution arguments for ``message``.
            **kwargs: Forwarded to the underlying logger method.

        Returns:
            None.
        """
        self._log("debug", context, message, *args, **kwargs)

    def info_with_context(self, context: str, message: str, *args, **kwargs) -> None:
        """Emit an INFO record carrying a context token.

        Thin convenience wrapper over :meth:`_log` with ``level="info"``.

        Args:
            context (str): Context token; folded into the message text.
            message (str): ``%``-style format string.
            *args: Positional substitution arguments for ``message``.
            **kwargs: Forwarded to the underlying logger method.

        Returns:
            None.
        """
        self._log("info", context, message, *args, **kwargs)

    def warning_with_context(self, context: str, message: str, *args, **kwargs) -> None:
        """Emit a WARNING record carrying a context token.

        Thin convenience wrapper over :meth:`_log` with ``level="warning"``.

        Args:
            context (str): Context token; folded into the message text.
            message (str): ``%``-style format string.
            *args: Positional substitution arguments for ``message``.
            **kwargs: Forwarded to the underlying logger method.

        Returns:
            None.
        """
        self._log("warning", context, message, *args, **kwargs)

    def error_with_context(self, context: str, message: str, *args, **kwargs) -> None:
        """Emit an ERROR record carrying a context token.

        Thin convenience wrapper over :meth:`_log` with ``level="error"``.

        Args:
            context (str): Context token; folded into the message text.
            message (str): ``%``-style format string.
            *args: Positional substitution arguments for ``message``.
            **kwargs: Forwarded to the underlying logger method.

        Returns:
            None.
        """
        self._log("error", context, message, *args, **kwargs)

    def critical_with_context(
        self, context: str, message: str, *args, **kwargs
    ) -> None:
        """Emit a CRITICAL record carrying a context token.

        Thin convenience wrapper over :meth:`_log` with ``level="critical"``.

        Args:
            context (str): Context token; folded into the message text.
            message (str): ``%``-style format string.
            *args: Positional substitution arguments for ``message``.
            **kwargs: Forwarded to the underlying logger method.

        Returns:
            None.
        """
        self._log("critical", context, message, *args, **kwargs)


# ═══════════════════════════════════════════════════════════════════════
#  SESSION
# ═══════════════════════════════════════════════════════════════════════
class QCSession:
    """Headless QC session engine hosting sensor/action/check/net dispatch.

    Replaces the archived terminal-era QC package's ``QCCore`` class. Where
    ``QCCore`` mixed in four handler classes (``TestQC``/``ActQC``/
    ``CheckQC``/``NetQC``) and dispatched on a hand-rolled registry lookup,
    ``QCSession`` dispatches through the unified ``QC.catalog`` tables
    populated by :func:`QC.registry_load.load_all`: a sensor key runs through
    :func:`QC.sensor_test.run_sensor`, and every other registered key runs its
    ``@qc_test``-decorated function directly. All operator interaction goes
    through :meth:`ask_operator`, which the web layer wires to a browser
    prompt via ``self.prompt``.

    Attributes:
        LOG_DIR (str): Class-level directory where the QC logger writes its log
            files (``"QC/logs"``).
        start_time (datetime.datetime): Wall-clock time this session was
            created.
        log (utils.oizom_logger.OizomLogger | _StdlibLoggerAdapter): Session
            logger built by :func:`QC.logger.build_qc_logger`; falls back to a
            plain :class:`logging.Logger` wrapped in
            :class:`_StdlibLoggerAdapter` if construction fails, so the
            ``*_with_context`` surface every caller relies on is always
            present.
        device_type (str): Active device profile key from ``DEVICE_PROFILES``
            (``"PD"``, ``"PS"``, ``"AQ"`` or ``"AQH"``).
        stage_name (str): Full name of the QC stage currently running (one of
            ``"Pre QC"``/``"Holo QC"``/``"Final QC"``; set by :meth:`start`).
        mac (str): Device MAC address resolved at :meth:`start`.
        sensor_status (dict[str, dict]): Per-test results keyed by catalog key;
            each entry holds ``passed``, ``value``, ``ts``, ``error``,
            ``display_range`` and ``display_value``.
        init_config (dict): Wrapper initialization value shared with the sensor
            pipeline; always empty in this port (see :mod:`QC.sensor_test`'s
            module docstring for why).
        d_payload (dict): Scratch payload data reserved for future handler use,
            carried over from the legacy session core.
        holo_done (bool): Whether this session's device has been registered on
            the Aikaan platform via :meth:`holo_register` — gates the first
            Final QC report generation in the web layer; reset by
            :meth:`start`.
        prompt: Operator prompt channel injected by the web app, or ``None``
            when headless — in which case :meth:`ask_operator` answers with its
            default so handlers never block.
    """

    LOG_DIR = "QC/logs"

    def __init__(self, store: ReportStore | None = None) -> None:
        """Initialize the logger, session state, report store and LED runtime.

        Builds the QC logger, falling back to a plain :class:`logging.Logger`
        wrapped in :class:`_StdlibLoggerAdapter` if construction fails — the
        adapter is required because every downstream consumer of ``self.log``
        (this class included, two lines below) calls the ``*_with_context``
        methods only ``utils.oizom_logger.OizomLogger`` exposes natively, and a
        bare stdlib logger lacks them. Writes a session banner to the log, resets
        per-session state, imports and registers every check/action/net test
        module via :func:`QC.registry_load.load_all`, and binds the shared report
        store (a fresh :class:`QC.report_store.ReportStore` when none is given).

        The status-LED runtime is started eagerly here — it needs no operator, so
        the LED shows QC mode (blue breathing) as soon as the session exists,
        matching the legacy session core's behavior. LED initialization failure
        (expected on a dev machine without the NeoPixel/GPIO hardware) is caught
        and logged as a warning rather than allowed to break session
        construction.

        Args:
            store (QC.report_store.ReportStore | None): Report store shared with
                the web layer; a new store is created when omitted. Defaults to
                ``None``.

        Returns:
            None.

        Note:
            ``self.prompt`` is left as ``None`` here and injected later by the web
            app. Until then :meth:`ask_operator` auto-answers with its default so
            handlers never block.
        """
        self.start_time: datetime = datetime.now()

        try:
            self.log = build_qc_logger(self.LOG_DIR)
        except Exception as e:  # noqa: BLE001 — ported session-core contract: a broken logger degrades to stdlib logging, it never blocks session construction
            raw_logger = logging.getLogger(__name__)
            raw_logger.warning("Logger init failed, using fallback: %s", e)
            self.log = _StdlibLoggerAdapter(raw_logger)

        self.log.info_with_context(DEFAULT, "=" * 60)
        self.log.info_with_context(
            DEFAULT, "New QC session — %s", datetime.now().isoformat(sep=" ")
        )
        self.log.info_with_context(DEFAULT, "=" * 60)

        self.device_type: str = "PD"
        self.stage_name: str = ""
        self.mac: str = ""
        self.particle_id: str = ""
        self.imei: str = ""
        self.sensor_status: dict[str, dict] = {}
        self.init_config: dict = {}
        self.d_payload: dict = {}
        self.holo_done: bool = False

        # Shared with the web layer — webapp owns HTTP, store owns state.
        self._report_store: ReportStore = store or ReportStore()
        # Injected by the web app. When absent, ask_operator answers with
        # the default so handlers never block.
        self.prompt = None

        # Import and register every check/action/net test module so the
        # catalog is complete before the first execute() call.
        load_all()

        # LED runtime needs no operator — start it eagerly so the status
        # LED shows QC mode as soon as the session is up. _ensure_led_runtime
        # only needs an object exposing `.log`, so a lightweight stand-in is
        # enough; it already swallows its own hardware-import failures, but
        # this call is wrapped too so a session can never fail to construct
        # over the LED runtime.
        try:
            _ensure_led_runtime(SimpleNamespace(log=self.log))
        except Exception as exc:  # noqa: BLE001 — hardware absent on dev must not break session construction
            self.log.warning_with_context(
                DEFAULT, "LED runtime init failed at session construction: %s", exc
            )

    # ─────────────────────────────────────────────────────────────────
    #  OPERATOR INTERACTION
    # ─────────────────────────────────────────────────────────────────
    def ask_operator(
        self,
        question: str,
        choices: list[str] | None = None,
        default: str = "yes",
        test: str | None = None,
    ) -> str:
        """Ask the operator a question via the web UI, or auto-answer when headless.

        Test functions call this (through ``ctx.ask``, which delegates to
        this bound method) whenever they need a human decision mid-test.
        When the web layer has injected a prompt channel (``self.prompt``),
        the question is forwarded to it and this call blocks until the
        operator answers. When no channel exists, ``default`` is returned
        immediately (with a warning logged) so handlers never block.

        Args:
            question (str): Question text shown to the operator.
            choices (list[str] | None): Allowed answer strings; ``None``
                becomes ``["yes", "no"]``.
            default (str): Answer returned when no prompt channel is wired,
                and the default offered in the UI. Defaults to ``"yes"``.
            test (str | None): Optional catalog key the prompt is
                associated with, forwarded to the prompt channel so the web
                UI can show which test is asking. Defaults to ``None``.

        Returns:
            str: The operator's chosen answer, or ``default`` when running
            headless.
        """
        choices = choices or ["yes", "no"]
        if self.prompt is None:
            self.log.warning_with_context(
                DEFAULT,
                "No operator prompt channel — auto-answering '%s' with '%s'",
                question,
                default,
            )
            return default
        return self.prompt.ask(question, choices=choices, default=default, test=test)

    # ─────────────────────────────────────────────────────────────────
    #  SESSION
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _get_mac_address() -> str:
        """Read the MAC address of the first active network interface on the RPi.

        Probes ``/sys/class/net/<iface>/address`` for ``eth0``, ``wlan0``,
        ``end0`` and ``eth1`` in that order and returns the first interface
        found. Any filesystem error is swallowed and reported as ``"N/A"``.

        Returns:
            str: Upper-cased MAC address (e.g. ``"B8:27:EB:12:34:56"``), or
            ``"N/A"`` if no known interface exists or reading fails.
        """
        try:
            for iface in ("eth0", "wlan0", "end0", "eth1"):
                p = pathlib.Path(f"/sys/class/net/{iface}/address")
                if p.exists():
                    return p.read_text().strip().upper()
        except Exception:  # noqa: BLE001, S110 — ported session-core contract: MAC lookup degrades to "N/A", it never raises or logs
            pass
        return "N/A"

    def _make_ctx(self) -> TestContext:
        """Build a fresh :class:`QC.context.TestContext` bound to this session's live state.

        Built anew on every call — rather than cached on the instance — so
        it always reflects ``self.log``/``self.record``/``self.device_type``/
        ``self.mac``/``self.particle_id`` as they stand right now, including any test double a
        caller has monkeypatched onto the instance after construction.

        Returns:
            TestContext: Context wired to this session's logger, result
            sink, operator prompt and device metadata.
        """
        return TestContext(
            log=self.log,
            record=self.record,
            ask=self.ask_operator,
            device_type=self.device_type,
            mac=self.mac,
            particle_id=self.particle_id,
        )

    def start(
        self,
        batch_id: str,
        device_id: str,
        operator: str,
        stage_name: str,
        device_type: str = "PD",
        mac_id: str = "",
        firmware_version: str = "",
        imei: str = "",
    ) -> dict:
        """Begin a new QC session from web-form input.

        Validates the device profile, resets per-session result state
        (``sensor_status``, ``holo_done``), resolves the device MAC address, and
        seeds the shared report store — first re-initializing it for the device
        profile via :meth:`QC.report_store.ReportStore.set_session`, then filling
        in the session metadata and the stage about to run — so the live report
        page can render it immediately. Called by the web layer when the operator
        submits the setup form; every later :meth:`execute` call reads the state
        established here.

        Args:
            batch_id (str): Production batch identifier entered by the operator.
            device_id (str): Device's Particle ID, recorded on the report as
                ``particle_id``.
            operator (str): Name of the QC operator running the session.
            stage_name (str): Full name of the QC stage being run (``"Pre QC"``,
                ``"Holo QC"`` or ``"Final QC"``); stored on the session and
                pushed verbatim to the report store.
            device_type (str): Device profile key from ``DEVICE_PROFILES``
                (``"PD"``, ``"PS"``, ``"AQ"`` or ``"AQH"``); an unrecognized value
                silently falls back to ``"PD"`` rather than raising. Defaults to
                ``"PD"``.
            mac_id (str): Operator-entered MAC address from the setup form. Used
                exactly as entered (surrounding whitespace stripped, case kept)
                when non-blank; blank or whitespace-only falls back to
                auto-detection via :meth:`_get_mac_address`, which itself yields
                ``"N/A"`` when no known interface is readable. Defaults to ``""``.
            firmware_version (str): Operator-selected firmware tag from the setup
                form (e.g. ``"v1.7"``). Recorded on the report as-is; not validated
                here. Defaults to ``""``.
            imei (str): Modem IMEI entered by the operator on the setup form.
                Recorded on the report as-is (surrounding whitespace stripped);
                not validated here, and blank is allowed so a device with no
                modem still starts a session. Unlike the MAC there is no
                auto-detect fallback: the IMEI comes from ``oizom-config``,
                which lives on the Pi host rather than in the container this
                suite runs in, so it is unreachable from here by design.
                Defaults to ``""``.

        Returns:
            dict: Snapshot of the report store taken after the session info and
            stage are applied, as produced by
            :meth:`QC.report_store.ReportStore.snapshot`.

        Note:
            Starting a session discards the results of any previous one on this
            instance — ``sensor_status`` is replaced with an empty dict and
            ``holo_done`` is cleared.
        """
        if device_type not in DEVICE_PROFILES:
            # A stale client still posting the pre-rename "ND" lands here; the
            # fallback is silent on the report, so at least the log says so.
            self.log.warning_with_context(
                SESSION, "Unknown device_type %r — falling back to PD", device_type
            )
            device_type = "PD"
        self.device_type = device_type
        self.stage_name = stage_name
        self.sensor_status = {}
        self.holo_done = False
        self.mac = mac_id.strip() or self._get_mac_address()
        self.particle_id = device_id.strip()
        self.imei = imei.strip()
        self._report_store.set_session(device_type)
        self._report_store.set_session_info(
            batch_id=batch_id,
            particle_id=device_id,
            operator=operator,
            mac_address=self.mac,
            qc_version=QC_VERSION,
            firmware_version=firmware_version,
            imei=self.imei,
        )
        self._report_store.set_stage(stage_name)
        self.log.info_with_context(
            SESSION,
            "Session start — batch=%s device=%s operator=%s stage=%s mac=%s imei=%s device_type=%s fw=%s",
            batch_id,
            device_id,
            operator,
            stage_name,
            self.mac,
            self.imei,
            device_type,
            firmware_version,
        )
        return self._report_store.snapshot()

    def execute(self, key: str, overrides: list | dict | None = None) -> bool:
        """Run one catalog entry by bare key and report pass/fail.

        Dispatches on which table ``key`` is registered in: a ``QC.catalog.SENSORS``
        key runs through :func:`QC.sensor_test.run_sensor` against either the
        operator's complete per-run config (``overrides``, used verbatim — full
        replace, no merging) or the sensor's catalog default; a ``QC.catalog.TESTS``
        key (a check/action/net test registered via ``@qc_test``) runs its function
        directly. A key in neither table is logged, recorded as a failed test via
        :meth:`record` so it still appears in the report, and reported as ``False``
        — it never raises for an unknown key.

        A fresh :class:`QC.context.TestContext` is built for every call (via
        :meth:`_make_ctx`) rather than reused, so it always reflects the session's
        current state.

        Args:
            key (str): Bare catalog key selected in the UI (e.g. ``"bme280"``,
                ``"usb"``).
            overrides (list | dict | None): The complete config to use for this run,
                exactly as edited in the UI's JSON editor — same shape as
                ``SENSORS[key]["config"]``. ``None``/empty falls back to the catalog
                default. Ignored for non-sensor keys. Defaults to ``None``.

        Returns:
            bool: ``True`` when the test passed; ``False`` on failure or an unknown
            key.

        Note:
            The override is deep copied before use so wrapper-side mutations never
            leak back into the stored override object; a structurally wrong config
            fails inside :func:`run_sensor` as a recorded test failure, never as an
            exception out of this method.
        """
        ctx = self._make_ctx()
        if key in SENSORS:
            config = copy.deepcopy(overrides) if overrides else SENSORS[key]["config"]
            return bool(run_sensor(key, config, ctx))
        if key in TESTS:
            return bool(TESTS[key]["func"](ctx))
        self.log.warning_with_context(DEFAULT, f"EXEC unknown test '{key}'")
        self.record(key, passed=False, value={}, error=f"unknown test '{key}'")
        return False

    def record(
        self,
        key: str,
        *,
        passed: bool,
        value: dict,
        error: str = "",
        display_range: str = "—",
        display_value: str = "",
        kind: str = "",
        samples: dict | None = None,
    ) -> None:
        """Persist a test result to ``sensor_status`` and push it to the live report.

        Ported from the legacy session core's ``_record``, adapted to the unified
        ``QC.catalog.CATALOG`` table (every entry — sensor, action, check or net —
        carries its own ``"display"`` and ``"kind"``, replacing the old
        four-registry fallback chain). Stamps the result with the current
        ``HH:MM:SS`` time, derives ``display_value`` from ``value`` when none is
        given (one ``key: value`` line per reading), and resolves the display name
        and kind from ``CATALOG``.

        Two records are written from the same data but with different shapes. The
        ``self.sensor_status[key]`` entry holds exactly ``passed``, ``value``,
        ``samples``, ``ts``, ``error``, ``display_range`` and ``display_value`` —
        no display name and no kind. ``samples`` stays out of the report store:
        the report shows the average, sensor registration sends the samples. The shared report store additionally receives
        ``display_name`` and ``kind``, and the name it gets is the catalog display
        with any trailing ``" (Model)"`` tag stripped: catalog displays follow
        ``SCAN.py``'s ``"Function (Model)"`` convention so the operator's tool
        shows the exact silicon, while the outgoing report is customer-facing and
        carries only the function. Names without a trailing tag pass through
        unchanged.

        The forward to :meth:`QC.report_store.ReportStore.update_test` is wrapped:
        any report-store failure is logged as a warning and never propagates, so a
        broken report cannot fail a test.

        Args:
            key (str): Catalog key of the test (sensor/action/check/net name).
            passed (bool): Whether the test passed.
            value (dict): Raw readings captured during the test; may be empty.
            error (str): Error description when the test failed. Defaults to
                ``""``.
            display_range (str): Expected-range text shown in the report.
                Defaults to ``"—"``.
            display_value (str): Pre-formatted value text for the report; built
                from ``value`` when empty. Defaults to ``""``.
            kind (str): Catalog kind, accepted so callers written against
                :meth:`QC.context.TestContext.record` (which always pass their own
                literal kind, e.g. ``kind="sensor"``) do not raise a
                ``TypeError``. ``CATALOG`` is authoritative: this value is used
                only when ``CATALOG`` supplies no kind for ``key`` — either
                because ``key`` is unregistered or because its entry's ``"kind"``
                is missing or empty. Defaults to ``""``.
            samples (dict | None): Per-reading values behind ``value``, one
                list per parameter (e.g. ``{"temp": [25.1, 25.2, ...]}``);
                sent by :meth:`allocate_sensor`. Defaults to ``None``, stored
                as ``{}``.

        Returns:
            None.

        Note:
            All arguments except ``key`` are keyword-only.
        """
        ts = datetime.now().strftime("%H:%M:%S")
        if not display_value and value:
            display_value = "\n".join(f"{k}: {v}" for k, v in value.items())
        entry = CATALOG.get(key, {})
        display_name = entry.get("display") or key
        # Catalog displays follow SCAN.py's "Function (Model)" convention so
        # the operator's tool shows the exact silicon; the outgoing report is
        # customer-facing and gets only the function — the trailing model tag
        # is stripped here. Names without a trailing tag pass through as-is.
        report_name = (
            display_name.rsplit(" (", 1)[0]
            if display_name.endswith(")")
            else display_name
        )
        resolved_kind = entry.get("kind", "") or kind
        self.sensor_status[key] = {
            "passed": passed,
            "value": value,
            "samples": samples or {},
            "ts": ts,
            "error": error,
            "display_range": display_range,
            "display_value": display_value,
        }
        try:
            self._report_store.update_test(
                key,
                passed=passed,
                value=value,
                ts=ts,
                error=error,
                display_range=display_range,
                display_value=display_value,
                display_name=report_name,
                kind=resolved_kind,
            )
        except Exception as e:  # noqa: BLE001 — ported session-core contract: a broken report must never fail a test
            self.log.warning_with_context(
                DEFAULT, "Failed to update report for %s: %s", key, e
            )

    # ─────────────────────────────────────────────────────────────────
    #  HOLO REGISTRATION (device onboarding after Holo QC passes)
    # ─────────────────────────────────────────────────────────────────
    def holo_register(
        self, particle_id: str, dm_id: str, batch_id: str = ""
    ) -> tuple[bool, str]:
        """Register the device with manager.oizom.com after Holo QC passes.

        Posts ``{"dt": 2, "dmid": <dm_id>, "batchid": <batch_id>, "note":
        <HOLO_NOTE>}`` to the Oizom manager HOLO endpoint for the given particle
        ID with a 15-second timeout. The batch id ties the registered device
        back to its production batch, and ``note`` carries the bench's hardware
        revision from the module-level :data:`HOLO_NOTE`. Once a request is
        sent, all failures — HTTP error statuses and raised exceptions alike —
        are captured and reported through the return value rather than raised;
        only the local precondition below raises.

        Args:
            particle_id (str): Device's Particle ID; coerced to ``str`` and
                whitespace-stripped before use. A blank (or ``None``) value is
                refused without any request, since it is the endpoint's last
                path segment.
            dm_id (str): Device-manager ID to bind to the device; whitespace
                is stripped before use.
            batch_id (str): Operator-entered production batch identifier;
                whitespace is stripped before use. Defaults to ``""`` so a
                caller that has no batch to hand still registers the device
                rather than failing.

        Returns:
            tuple[bool, str]: ``(ok, message)`` where ``ok`` is ``True``
            only for a 2xx response, and ``message`` is operator-facing
            text — either the QC-complete/reboot instruction or the failure
            reason (HTTP error status, or transport/other exception).

        Raises:
            HoloPrecondition: When ``particle_id`` is blank — no session was
                started — so the caller can report a client-side 4xx rather
                than an upstream failure. Nothing has been sent.
        """
        # The particle id IS the last path segment of the endpoint, so a blank
        # one silently posts to /v2/qc/init/ — a route that does not exist. The
        # platform then answers 404 "Cannot POST /v2/qc/init/", which reads as a
        # server outage when the real cause is a session that was never started
        # (particle_id is only ever set by QCSession.start). Refuse here rather
        # than send a URL we know is malformed.
        particle_id = str(particle_id or "").strip()
        if not particle_id:
            self.log.error_with_context(
                HOLO, "HOLO register refused: blank particle_id"
            )
            raise HoloPrecondition(
                "No Particle ID in this session — start a QC session on the setup form before registering."
            )

        try:
            url = OIZOM_MANAGER + HOLO_API + particle_id
            self.log.info_with_context(
                HOLO,
                "HOLO register particle_id=%s dm_id=%s batch_id=%s note=%s",
                particle_id,
                dm_id,
                batch_id,
                HOLO_NOTE,
            )
            payload = {"dt": 2, "dmid": dm_id.strip(), "note": HOLO_NOTE}
            if (
                batch_id.strip()
            ):  # an empty batchid is rejected/misfiled upstream; omit the key instead
                payload["batchid"] = batch_id.strip()
            r = requests.post(url, json=payload, headers=API_HEADERS, timeout=15)
            self.log.info_with_context(
                HOLO, "HOLO POST status=%s body=%s", r.status_code, r.text[:200]
            )
            if r.ok:
                return True, f"QC complete for {particle_id}. Please reboot the device."
            # Include the platform's own explanation: this message is what the
            # device-id dialog shows, and a bare status line cannot distinguish
            # a rejected payload from an unknown device or an expired token.
            detail = (r.text or "").strip()[:300]
            return False, f"Registration failed: {r.status_code} {r.reason}" + (
                f" — {detail}" if detail else ""
            )
        except Exception as exc:  # noqa: BLE001 — ported session-core contract: registration failure reports through the return value, it never raises
            self.log.critical_with_context(HOLO, "HOLO registration error")
            return False, f"Registration error: {exc}"

    # ─────────────────────────────────────────────────────────────────
    #  SENSOR ALLOCATION (bind a physical sensor ID to its QC reading)
    # ─────────────────────────────────────────────────────────────────
    def allocate_sensor(self, key: str, sensor_id: str) -> tuple[bool, str]:
        """Send one sensor's QC reading to the socket server under its sensor ID.

        Web port of the terminal tool's per-sensor allocation (``Old_QC.py``
        ``indirect``): after a sensor test the operator enters the physical
        sensor's ID and ``{"SensorId", "CommandId", "SensorData"}`` is posted
        to ``OIZOM_SOCKET + QC_API``. ``CommandId`` is the terminal tool's
        command letter from :data:`LEGACY_COMMAND_IDS` (the catalog key when the
        sensor has none) in a one-element list, the same shape the terminal
        tool sent (e.g. ``["d"]``); the Node-RED flow picks the Google Sheet tab
        from it. ``SensorData`` is the JSON text of every individual reading,
        one list per parameter (``{"temp": [v1, v2, v3, v4, v5]}``), falling
        back to the averaged value when no samples were recorded. Failures are
        reported through the return value, never raised.

        Args:
            key (str): Catalog key of a sensor test recorded in ``sensor_status``.
            sensor_id (str): Operator-entered physical sensor ID; whitespace is
                stripped before use.

        Returns:
            tuple[bool, str]: ``(ok, message)`` where ``ok`` is ``True`` only
            for a 2xx response and ``message`` is operator-facing text.
        """
        return self._send_allocation(
            key, sensor_id, LEGACY_COMMAND_IDS.get(key, key), self._readings(key)
        )

    def allocate_combined(self, command_id: str, sensor_id: str) -> tuple[bool, str]:
        """Register a sensor pair as ONE physical sensor under its combined CommandId.

        Web port of the terminal tool's paired commands (``k``, ``ae``,
        ``ae2``): the members listed in :data:`COMBINED_REGISTRATIONS` share one
        Sensor ID and one post, and their recorded readings are merged by
        :func:`_merge_samples`. The members were tested in their own runs, so
        their samples are not simultaneous the way the terminal tool's single
        combined read was. Failures — including a merge that raises — are
        reported through the return value, never raised.

        Args:
            command_id (str): A :data:`COMBINED_REGISTRATIONS` key, e.g. ``"ae"``.
            sensor_id (str): Operator-entered physical sensor ID; whitespace is
                stripped before use.

        Returns:
            tuple[bool, str]: ``(ok, message)`` where ``ok`` is ``True`` only
            for a 2xx response and ``message`` is operator-facing text.
        """
        keys = COMBINED_REGISTRATIONS[command_id]["keys"]
        try:
            data = _merge_samples({k: self._readings(k) for k in keys})
        except Exception as exc:  # noqa: BLE001 — allocation failure reports through the return value, it never raises
            self.log.error_with_context(
                ALLOC, "Combined allocation merge failed for %s: %s", command_id, exc
            )
            return False, f"Could not combine readings: {exc}"
        return self._send_allocation(command_id, sensor_id, command_id, data)

    def _readings(self, key: str) -> dict:
        """Return a recorded sensor's per-sample readings, else its averages.

        Args:
            key (str): Catalog key of a sensor recorded in ``sensor_status``.

        Returns:
            dict: ``samples`` (one list per parameter) when recorded, otherwise
            the averaged ``value``; ``{}`` for a key never recorded.
        """
        result = self.sensor_status.get(key, {})
        return result.get("samples") or result.get("value") or {}

    def _send_allocation(
        self, label: str, sensor_id: str, command_id: str, data: dict
    ) -> tuple[bool, str]:
        """Post one ``{"SensorId", "CommandId", "SensorData"}`` payload to the socket server.

        Shared by :meth:`allocate_sensor` and :meth:`allocate_combined`.

        Args:
            label (str): Catalog key or combined CommandId, used only in logs.
            sensor_id (str): Operator-entered physical sensor ID; whitespace is
                stripped before use.
            command_id (str): Terminal-tool command letter; sent as a
                one-element list, the shape the terminal tool sent.
            data (dict): Readings sent as JSON text in ``SensorData``.

        Returns:
            tuple[bool, str]: ``(ok, message)`` where ``ok`` is ``True`` only
            for a 2xx response and ``message`` is operator-facing text.
        """
        payload = {
            "SensorId": sensor_id.strip(),
            "CommandId": [command_id],
            "SensorData": json.dumps(data),
        }
        try:
            self.log.info_with_context(ALLOC, "Sensor allocation payload=%s", payload)
            r = requests.post(
                OIZOM_SOCKET + QC_API, json=payload, headers=API_HEADERS, timeout=15
            )
            self.log.info_with_context(
                ALLOC,
                "Sensor allocation status=%s body=%s",
                r.status_code,
                r.text[:200],
            )
            if r.ok:
                return True, "Registered"
            detail = (r.text or "").strip()[:300]
            return False, f"{r.status_code} {r.reason}" + (
                f" — {detail}" if detail else ""
            )
        except Exception as exc:  # noqa: BLE001 — allocation failure reports through the return value, it never raises
            self.log.error_with_context(
                ALLOC, "Sensor allocation error for %s: %s", label, exc
            )
            return False, f"Registration error: {exc}"
