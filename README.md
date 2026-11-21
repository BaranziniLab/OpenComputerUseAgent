# Open Computer Use Agent

A fallback for desktop control on computers and apps that Biorouter Copilot does not support,
built on Open Computer Use. macOS 14 or later, Windows x64, and Linux on X11.

Current version: **0.1.0**

This is a Biorouter extension (`.brxt`). It is **not** Biorouter Copilot and does not replace it.
Biorouter Copilot is built into Biorouter and should be your first choice. Install this extension
only when Biorouter Copilot cannot help on your computer or with a particular app.

## When to use it

Try Biorouter Copilot first. Use this fallback in the cases below.

| Situation | Biorouter Copilot (built in) | Open Computer Use Agent (this extension) |
|---|---|---|
| Linux with at-spi2-core older than 2.52 (Ubuntu 22.04, Debian 12, RHEL and Rocky 9) | Reports `unsupported_environment` | Works through the distro's own Python AT-SPI bridge. Tested on Ubuntu 22.04, Debian 12, Rocky 9 and Ubuntu 24.04 in an X11 session (Xvfb with openbox); full GNOME and KDE sessions are not measured yet. |
| An app whose controls Biorouter Copilot cannot read or operate (older toolkits, some proprietary apps) | May fail or return no usable elements | A different accessibility engine. It sometimes works where the built in one does not. |
| macOS 13 | Supported | Not supported (needs macOS 14 or later). Use Biorouter Copilot. |
| macOS 14 and later, Windows x64, Ubuntu 24.04 | Supported | Supported, for apps Biorouter Copilot cannot drive |
| Wayland pointer input and capture | Not offered | Not offered either. Use an X11 session. |
| No signed in desktop session | Unavailable | Unavailable |
| `biorouter serve` in a browser | | Does not work: its question to you can only be answered in the desktop app or the terminal |

## What's inside

- **The wrapper**, a small MCP server written in standard library Python (3.9 or later, no
  dependencies). It is what Biorouter talks to. It pins the helper's tools and their arguments,
  scrubs the helper's environment, asks you once per chat, refuses what Biorouter Copilot
  refuses, and owns the helper's lifetime.
- **The helper**, Open Computer Use 1.1.5 (MIT) with the patches in
  `vendor/open-computer-use/patches/`, built for five targets: `darwin-arm64`, `darwin-x64`,
  `win32-x64`, `linux-x64` and `linux-arm64`. On macOS it is a separate app, "Open Computer Use
  Agent", which holds the Accessibility and Screen Recording permissions. Biorouter needs neither.
- Every helper ships in the one `.brxt`, so nothing is downloaded after install.

## Requirements

- Biorouter 1.94.6 or later, and `uv` (Biorouter installs extensions with it).
- **macOS** 14 or later.
- **Windows** 10 or 11 on x64. Windows on Arm is not supported yet.
- **Linux** on X11 (x64 or arm64), signed in to a desktop session, with the distro's Python,
  PyGObject, AT-SPI and GTK 3 packages:

  | Family | Command |
  |---|---|
  | Debian, Ubuntu (apt) | `sudo apt install python3 python3-gi gir1.2-atspi-2.0 gir1.2-gtk-3.0 at-spi2-core` |
  | Fedora, RHEL, Rocky (dnf) | `sudo dnf install python3 python3-gobject at-spi2-core gtk3` |
  | openSUSE (zypper) | `sudo zypper install python3 python3-gobject typelib-1_0-Atspi-2_0 typelib-1_0-Gtk-3_0 at-spi2-core` |
  | Arch (pacman) | `sudo pacman -S python python-gobject at-spi2-core gtk3` |

  The extension never installs packages itself. `setup_status` says which are missing.

## Install in Biorouter

- **Install from file:** download `opencomputeruseagent.brxt` from this repository's releases and
  drop it into Biorouter's **Extensions → Add extension** dialog.
- **Terminal:** `biorouter extension install ./opencomputeruseagent.brxt`. In Biorouter 1.94.6 the
  command takes a local file, not a URL.
- **Browse marketplace:** once it is listed in BAAM, install it from the marketplace in the app.

Then turn the extension on. Its tools reach the model as `opencomputeruseagent__<tool>`.

## Grant permissions on macOS

macOS asks for two permissions, and both belong to the helper app **"Open Computer Use Agent"**,
never to Biorouter:

1. Ask Biorouter to run `open_permission_setup`, or open **System Settings → Privacy & Security**.
2. Under **Accessibility**, allow **Open Computer Use Agent**.
3. Under **Screen Recording**, allow **Open Computer Use Agent**.
4. Run `setup_status`; it reports both as granted.

Windows and Linux need no permission grant.

## Usage

Ask in plain language, for example:

- "Biorouter Copilot says this computer is not supported. Use Open Computer Use Agent to open the
  settings dialog in gedit and turn on line numbers."
- "Read the values in the second table of the LabWare window."
- "Fill the sample ID field in the instrument app with 2026-0417 and press Apply."

The first time it looks at your apps in a chat, a question appears in that chat. Answer it there.

## Tools

1. `list_apps`: lists running and recently used apps. Biorouter's own windows are left out.
2. `get_app_state`: an app's key window as an accessibility tree and a screenshot.
3. `screen_capture`: one app window by title. Whole display capture is refused.
4. `click`: an element by index, or a point of the app's latest screenshot.
5. `perform_secondary_action`: a secondary accessibility action an element offers.
6. `scroll`: scrolls an element by pages.
7. `drag`: between two points of the app's latest screenshot. May move your pointer.
8. `type_text`: types into the focused field. Never into password fields or terminals.
9. `press_key`: a key or chord. Copy, cut and paste chords are refused.
10. `set_value`: sets and verifies an element's value. Never a password field.
11. `setup_status`: whether this computer can run the fallback, and why not. Needs no consent.
12. `open_permission_setup`: macOS only. Opens the helper app's permission window.

## How consent works

Biorouter's own tool approval applies first, as for any installed extension:

| Mode | Biorouter's approval | Then this extension |
|---|---|---|
| Chat only | No tool runs | |
| Manual | Every call shows the approval card, unless you chose Always allow for that tool | Asks once per chat |
| Smart | Looking tools run; acting tools and `open_permission_setup` ask | Asks once per chat |
| Completely autonomous | Every call runs with no card | Asks once per chat |

The extension's question has three answers: **Don't allow** (the default), **Only allow looking**
and **Allow looking and acting**. If you allow only looking, the first action asks again.

- The answer holds for that chat only. It ends after 15 minutes without use, when the extension
  restarts, when you dismiss the question, or when Biorouter cancels one of that chat's calls.
- In the desktop app the question is a card with a list and a Submit button. **Don't allow** is
  already chosen, so Submit without changing it refuses. The card has no separate dismiss button.
- In the terminal it needs `BIOROUTER_CLI_CLASSIC=1`. Biorouter's default terminal interface
  cannot show the question and would cancel the whole turn, so the extension notices that
  interface, asks nothing, and tells the model why; the turn goes on.
- Scheduled and unattended runs cannot answer it, so the extension refuses there.
- **Stop does not interrupt a running call.** Biorouter 1.94.6 does not tell an extension when you
  press Stop during an ordinary call. The action already running finishes (actions take about a
  second; nothing waits longer than 60 seconds), Biorouter drops its late result, and the next
  call in that chat still has its answer. Because the extension runs one action at a time per
  chat and refuses an action sent while another is running, a batch of actions cannot keep going
  after Stop: at most the one running action completes. Stop while the question is open does
  cancel it. When Biorouter does send a cancel (on its own timeout, and in any version that passes
  Stop on), the extension stops the call, ends the chat's access and asks again next time.

## What it refuses

- Biorouter's own windows (by process ancestry, the running Biorouter's install folder and its
  bundle identifiers), and the helper app's own windows.
- System security and password prompts, password stores and notification centres.
- Typing into, setting or clearing password fields (in the helper itself, which can see focus).
- Keys and text while macOS secure keyboard input is on.
- Text that looks like a secret: private keys, AWS keys, GitHub, OpenAI, Anthropic, Slack,
  Google and Hugging Face tokens, and JSON web tokens.
- Keys, text, drops and menu actions in terminals. Looking, clicking and scrolling there are allowed.
- Changes in privacy, security, users and sign in settings. Looking is allowed.
- Copy, cut and paste chords, and whole display capture.
- An action without a fresh `get_app_state` of that app in the same chat. A failed or uncertain
  action counts as a change.
- An action sent while another action of the same chat is still running. Send them one at a time.
- A second try after a timeout: the helper is ended and the answer says the outcome is uncertain.

## With Biorouter Copilot

Use one at a time. Both are told so: this extension's instructions tell the model to use it only
when Biorouter Copilot is not in the chat, reported `unsupported_environment` or
`computer_use_unsupported_environment`, cannot operate the app and you agree to try the fallback,
or when you ask for it by name. The two also share one desktop lock: while one holds it for a
chat, the other refuses.

## Privacy

Screenshots and window text go to the chat's model, which may run outside your institution. The
extension cannot tell whether a chat's model is private, so the question always says so. It is a
Public extension in Biorouter's privacy tiers. Biorouter scans every tool result for secrets
before the model sees it.

## Troubleshooting

- **Linux: "needs a signed in desktop session".** Run Biorouter from your desktop session, where
  `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS` and `DISPLAY` are set.
- **Linux: missing packages.** Run `setup_status` and install what it lists (see Requirements).
- **Wayland.** Clicks by position, drags and window capture need X11. Choose an X11 session at
  sign in.
- **macOS: permission seems granted but the helper still asks.** An ad hoc signed development
  build gets its own entry in System Settings. Remove the old "Open Computer Use Agent" entries
  and grant the installed one again.
- **Terminal: "Biorouter's default terminal interface cannot show that question".** Start the
  terminal with `BIOROUTER_CLI_CLASSIC=1`, for example `BIOROUTER_CLI_CLASSIC=1 biorouter session`,
  or use the desktop app.
- **macOS: an "Open Computer Use Agent" process stays in Activity Monitor.** It is the helper app
  that holds the permissions. It quits by itself after ten minutes without use, and when the
  extension stops.

## Build and test

```sh
python3 scripts/vendor_source.py --verify          # the vendored tree matches its manifest
python3 scripts/check_vendored.py                   # and survives a fresh checkout
python3 scripts/build_helper.py build darwin-arm64  # macOS, Swift 6.2 or later (ad hoc signed)
scripts/build_go_in_docker.sh                       # linux-x64, linux-arm64, win32-x64 with Go 1.26.8
python3 scripts/build_brxt.py build --payloads build/payload
uv run --group test pytest                          # wrapper unit tests
python3 tests/test_helper_protocol.py build/payload/<target>
python3 tests/test_wrapper_protocol.py build/payload/<target>
python3 tests/test_macos_agent_lifetime.py build/payload/darwin-arm64   # idle exit, no grant needed
python3 tests/test_linux_fixture.py --docker        # the GTK fixture on four distros
python3 tests/test_biorouter_install.py --bin <biorouter folder> --root <empty folder>
python3 tests/test_biorouter_cli.py --bin <biorouter folder> --root <empty folder>
```

Two decisions differ from the usual Biorouter extension layout, both so that installing needs no
network on the old systems this is for: the project asks for Python 3.9 or later (asking for 3.11
made `uv` download a Python on Ubuntu 22.04 and Rocky 9), and it is not an installable package
(`[tool.uv] package = false`, no build backend), so `entry_point` is the launcher script
`opencomputeruseagent.py` rather than a console script.

Releases are cut on the release Mac with `scripts/release.sh <version>`, which signs and
notarizes the macOS helpers with the Developer ID from that Mac's Keychain. No signing secret is
stored in this repository or its Actions secrets.

```
LICENSE  NOTICE  README.md  manifest.json  pyproject.toml  uv.lock  opencomputeruseagent.py
src/opencomputeruseagent/      the wrapper (server, dispatch, helper, consent, guard, ...)
vendor/open-computer-use/      pristine upstream source, pin.json, patches/, source-manifest.json
scripts/                       vendoring, brand and version checks, builds, release
fixtures/                      the GTK, AppKit and WinForms test apps
tests/                         unit tests, protocol tests and the fixture drives
```

## License

Apache License 2.0 (see `LICENSE`). The bundled Open Computer Use is MIT licensed and includes
code derived from Cua Driver and yabai, both MIT; see `NOTICE` and `vendor/open-computer-use/`.

## Authors

Baranzini Lab, University of California, San Francisco.
