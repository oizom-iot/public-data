"""Execution context handed to every QC test function."""


class TestContext:
    """Execution context handed to every QC test function.

    Bundles the session services a test may touch — logging, result
    recording, and operator prompting — so test functions stay free of
    infrastructure concerns and can run identically in web and headless
    sessions.

    Attributes:
        log: OizomLogger for the QC session, exposing ``*_with_context``
            methods.
        record: Callable that persists a test's outcome to the result sink.
        _ask: Callable bound to the operator prompt channel, or ``None``
            when the session runs headless.
        device_type (str): Device profile code (PD/PS/AQ/AQH).
        mac (str): Device MAC address string.
        particle_id (str): Device's Particle ID from the setup form.
    """

    def __init__(self, log, record, ask, device_type="", mac="", particle_id=""):
        """Bind the session services this context exposes.

        Args:
            log: OizomLogger-style logger exposing ``*_with_context`` methods.
            record: Callable used to persist a test's outcome; invoked as
                ``record(name, *, passed, value, error="", display_range="—",
                display_value="", kind="", samples=None)``.
            ask: Callable used to prompt the operator, or ``None`` when no
                prompt channel is available (headless run). Signature:
                ``ask(question, choices=None, default="yes", test=None)``.
            device_type (str): Device profile code (PD/PS/AQ/AQH). Defaults
                to ``""``.
            mac (str): Device MAC address string. Defaults to ``""``.
            particle_id (str): Device's Particle ID from the setup form.
                Defaults to ``""``.
        """
        self.log = log
        self.record = record
        self._ask = ask
        self.device_type = device_type
        self.mac = mac
        self.particle_id = particle_id

    @property
    def is_helium(self) -> bool:
        """Whether the device under test is a Helium board.

        The one place the board variant is decided. A Particle ID starting
        with ``H`` (any case) is a Helium board: no MCP230XX, so no
        ``/dev/i2c-6``, and the siren and TM1638 display hang off Pi GPIO.
        Anything else (``P``-prefixed, blank) takes the MCP path.

        Returns:
            bool: ``True`` for a Helium board, ``False`` otherwise.
        """
        return self.particle_id.strip().upper().startswith("H")

    def ask(self, question, choices=None, default="yes", test=None):
        """Ask the operator via the web prompt; return the default when headless.

        When no prompt channel is bound (``_ask`` is ``None``), the question is
        logged and ``default`` is returned immediately instead of blocking.

        Args:
            question (str): Prompt text shown to the operator.
            choices (list | None): Optional set of allowed answers, forwarded
                to the prompt channel. Defaults to ``None``.
            default (str): Answer returned when no prompt channel is bound.
                Defaults to ``"yes"``.
            test (str | None): Optional test key the prompt is associated
                with, forwarded to the prompt channel. Defaults to ``None``.

        Returns:
            str: The operator's answer, or ``default`` when headless.
        """
        if self._ask is None:
            self.log.info_with_context(
                "QC",
                "No prompt channel — auto-answering '%s' with '%s'",
                question,
                default,
            )
            return default
        return self._ask(question, choices=choices, default=default, test=test)
