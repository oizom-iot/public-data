# ozdev

Terminal tool for inspecting an Oizom device across both Aikaan device managers
and the admin platform, then getting onto it.

**macOS / Linux:**

```sh
curl -fsSL https://raw.githubusercontent.com/oizom-iot/public-data/main/ozdev/install.sh | sh
```

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/oizom-iot/public-data/main/ozdev/install.ps1 | iex
```

Then:

```sh
ozdev login
ozdev <device-name>
```

No Node, no git, no GitHub account needed. The installer picks the binary for
your machine, verifies it against the release's `SHA256SUMS`, and installs it to
`~/.local/bin`. After that, `ozdev update` keeps it current.

Builds live on the [releases page](https://github.com/oizom-iot/public-data/releases)
under `ozdev-v*` tags. Source is private at `OzFirmware/oizom-devcli`.
