# Vendored Open Computer Use

`source/` is the MIT licensed upstream project, [iFurySt/open-computer-use](https://github.com/iFurySt/open-computer-use),
at the commit `pin.json` names (1.1.5, `8ec5cdbc`), with the documentation assets listed under
`excluded` in `source-manifest.json` left out. A build reads it and needs no network, so an
upstream repository that is deleted, force pushed or altered cannot change a helper built here.

The files in `source/` are **pristine**: the patches are not applied to them. `patches/` is
applied in name order to a throwaway copy at build time (`scripts/build_helper.py stage`), which
gets its own `git init` first, because `git apply` inside this repository's ignored `build/`
folder silently drops every new file hunk. Keeping the two apart makes an upstream update a
matter of replacing the tree and re-applying the reviewed patches.

`source-manifest.json` holds a SHA-256 of every vendored file and one digest over the tree, and
the build verifies it in both directions before the bytes become build input.
`scripts/check_vendored.py` also checks that every file survives a fresh checkout (no ignore rule
drops it, no line ending conversion rewrites it).

Left out, as in Biorouter's earlier vendoring: artwork upstream extracted from another vendor's
application (upstream's MIT licence does not cover it), that vendor's plugin archive notes,
upstream's root `.gitattributes`, and one README screenshot whose photograph is not shown to be
upstream's own. None of them is a build input.

## The patches (revision ocua-1)

| Patch | What it does |
|---|---|
| 0001-branding | The app is "Open Computer Use Agent", `edu.ucsf.baranzinilab.opencomputeruseagent` (development `...opencomputeruseagent.dev`), with the build script's own drawn cursor. |
| 0002-native-capture-isolation | The tenth tool `screen_capture`, `doctor --json` readiness, per connection state and cancellation, a mandatory socket namespace, a 0600 socket, strict agent identity, strict numeric arguments, verified `set_value`, measured scrolling, and the distro `/usr/bin/python3` on Linux. |
| 0004-agent-prompts | The helper's own text says "Open Computer Use Agent" and "fallback". |
| 0005-accessibility-guidance | The Accessibility message names the helper app. |
| 0006-offline-build | Drops the remote libwebp package so the build is offline; PNG and JPEG remain. |
| 0007-ten-tool-contract | MCP advertises and accepts exactly the ten pinned tools; Locked Use is removed; SkyLight input is refused; Windows chords use real keys only under the global input flag. Its Go contract tests compare sorted lists. |
| 0008-password-field-writes | Password fields never receive `type_text`, `press_key` or `set_value` (macOS `AXSecureTextField`, Windows `IsPassword` and `ES_PASSWORD`, Linux role `password text`), and Linux `type_text` writes into the focused field, never the window's first one. |
| 0009-app-agent-idle-exit | The macOS app agent exits after ten minutes with no connection and no visible window. |
| 0010-linux-python-and-help | The Linux interpreter path is a variable the upstream tests point at their stub, and the CLI help no longer lists Locked Use. |
| 0011-type-text-whitespace | `type_text` types its text exactly; trimming dropped leading and trailing spaces. |

## Checking for upstream changes

```sh
python3 scripts/check_upstream.py          # summary
python3 scripts/check_upstream.py --json   # machine readable
```

Read only. What it is for is the list of upstream files the patches also touch, since those are
what will conflict on re-vendoring.

## Updating the vendored tree

```sh
git clone https://github.com/iFurySt/open-computer-use.git /tmp/ocu && git -C /tmp/ocu checkout <commit>
$EDITOR vendor/open-computer-use/pin.json          # upstream_commit, upstream_version, patch_revision
python3 scripts/vendor_source.py --from /tmp/ocu
python3 scripts/build_helper.py stage              # the patches must apply
```

Then update `UPSTREAM_VERSION`, `UPSTREAM_COMMIT` and `PATCH_REVISION` in
`src/opencomputeruseagent/__init__.py` (`scripts/version.py check` compares them with the pin),
rebuild every target, and run the protocol and fixture tests.
