# LSP-tads3tools

Sublime Text LSP client for the [vscode-tads3tools](https://github.com/toerob/vscode-tads3tools)
TADS3 language server. Provides hover documentation, completions, go-to-definition, find
references, diagnostics, and parse-progress feedback for TADS3 projects.

## Requirements

| Requirement | Notes |
|-------------|-------|
| Sublime Text 4 | Build 4075 or later |
| [LSP](https://packagecontrol.io/packages/LSP) package | The base LSP client for Sublime Text |
| frobtads | Provides `t3make` (build) and `frob` (run); install via your package manager |

## Installation

### 1. Install the LSP package

Open the Command Palette (`Cmd+Shift+P` / `Ctrl+Shift+P`) and run
**Package Control: Install Package**, then select **LSP**.

### 2. Install LSP-tads3tools

Clone or copy this repository into your Sublime Text `Packages` directory:

```sh
# macOS
cd ~/Library/Application\ Support/Sublime\ Text/Packages
git clone https://github.com/toerob/subl-tads3tools LSP-tads3tools

# Linux
cd ~/.config/sublime-text/Packages
git clone https://github.com/toerob/subl-tads3tools LSP-tads3tools

# Windows
cd "%APPDATA%\Sublime Text\Packages"
git clone https://github.com/toerob/subl-tads3tools LSP-tads3tools
```

The folder must be named **`LSP-tads3tools`** exactly — Sublime Text loads it by
directory name.

### 3. Install the language server binary

Open a TADS3 file (or any file), open the Command Palette, and run:

```
Tads3: Install Server
```

This queries the [latest vscode-tads3tools release](https://github.com/toerob/vscode-tads3tools/releases/latest)
for its actual binary assets and shows a quick panel to choose from, with the asset
matching your OS and CPU architecture (inferred automatically) marked as recommended and
listed first. The chosen binary is downloaded and saved to:

| Platform | Location |
|----------|----------|
| macOS / Linux | `~/Library/Application Support/Sublime Text/Local/tads3tools/bin/` (macOS) |
| Linux | `~/.config/sublime-text/Local/tads3tools/bin/` |
| Windows | `%APPDATA%\Sublime Text\Local\tads3tools\bin\` |

**Manual alternative:** download `vscode-tads3tools-server-<platform>` from the releases
page, place it in the directory above, and make it executable:

```sh
chmod +x .../Local/tads3tools/bin/vscode-tads3tools-server-*
```

If the language server was already running, installing a binary restarts it
automatically — no configuration changes needed, since `${server_path}` is re-resolved
from disk on every session start. Otherwise, open a `.t` file (or restart Sublime Text)
to start it for the first time.

## Configuration

Open the settings file via:

**Preferences → Package Settings → LSP-tads3tools → Settings**

TADS3 system headers and library files are resolved from the `-source`/`-lib` entries
in your project's `.t3m` makefile, not from LSP settings — there's no `include`/`lib`
path to configure. The one setting the server actually reads is
`enablePreprocessorCodeLens`:

```json
{
    "settings": {
        "tads3": {
            "enablePreprocessorCodeLens": false
        }
    }
}
```

### Changing the interpreter

The `Tads3: Run` command launches your compiled game through `frob` by default.
Override it with the `interpreter` key:

```json
{
    "interpreter": "qtads"
}
```

`frob` is a curses-based terminal interpreter, so it's launched in a real terminal
window (Terminal.app on macOS, `gnome-terminal`/`konsole`/`xfce4-terminal`/`xterm` on
Linux, `cmd` on Windows) rather than Sublime's build-output panel — the panel isn't a
real tty, which is why running a curses program through it fails with
`Error opening terminal: unknown`. GUI interpreters like `qtads` and `htmltads` don't
have this problem and are launched directly.

### Using a custom server build

If you have a local build of the language server from the vscode-tads3tools repository,
point the plugin at it with `server_path`:

```json
{
    "server_path": "/path/to/vscode-tads3tools/server/bin/vscode-tads3tools-server-macos-arm64"
}
```

## Verifying it works

1. Open a TADS3 project file (`*.t`).
2. After 2–3 seconds the status bar should show parse progress — `[tads3 12/48]` —
   and then a message like:
   ```
   [tads3tools] Parsed 71 files in 2387 ms
   ```
3. Hover over a class name or function — you should get a documentation pop-up.
4. Right-click a symbol and choose **LSP → Go to Definition** (or press `F12`).

If parsing does not start automatically, run **Tads3: Parse** from the Command Palette
to trigger it manually and watch the status bar for errors.

## File type detection

`.t` files are detected as TADS3 by checking the first few lines for patterns like
`#charset`, `#include`, `gameMain:`, or `versionInfo:`. Files that do not match
(e.g. Perl test files) keep their original syntax.

`.t3m` makefile files are always detected as TADS3 Makefile.

`.h` header files are **not** auto-detected as TADS3 to avoid conflicting with C/C++
headers. To use TADS3 syntax for a header file, set it manually via
**View → Syntax → TADS3**.

## Commands

All commands are available from the Command Palette (`Cmd+Shift+P` / `Ctrl+Shift+P`):

| Command | Description |
|---------|-------------|
| `Tads3: Install Server` | Download the server binary for your platform (run once after install) |
| `Tads3: Parse` | Re-parse the full project (e.g. after adding source files or changing the makefile) |
| `Tads3: Abort Parse` | Cancel a parse that is taking too long |
| `Tads3: Build` | Compile with `t3make`; errors appear in the build output panel |
| `Tads3: Run` | Launch the compiled game through the configured interpreter |
| `Tads3: Select Makefile` | Re-prompt which `.t3m` to use for projects with multiple makefiles |

## Build system

The package ships `TADS3.sublime-build`, so **Cmd+B** / **Ctrl+B** runs `Tads3: Build`
directly — no manual build system selection needed, as long as **Tools → Build System**
is set to **Automatic** (the default) and you're in a `.t` file. **Cmd+Shift+B** /
**Ctrl+Shift+B** additionally offers **Run** and **Parse** as build variants, mapping to
`Tads3: Run` and `Tads3: Parse`.

Since selection is based on the *current view's* syntax, Cmd+B won't auto-select this
build system while editing a non-TADS3 file (e.g. the `.t3m` makefile itself) even
within the same project — switch to a `.t` file first, or pick **TADS3** manually via
Tools → Build System.

## Automatic parsing

If a project has exactly one `.t3m` file, it's selected and *fully* parsed
automatically the first time you open or switch to a file anywhere under that folder —
no command needed.

After that baseline parse, saving a file (not just `.t` files — e.g. `.h` headers count
too) triggers a **targeted** re-parse of just that file instead of the whole project,
debounced by 500ms so a burst of saves (e.g. Save All) collapses into a single request
covering every file saved in that window — each one gets re-parsed, not just the last.
This only rebuilds symbols for the files that changed; it doesn't pick up brand new
files or symbols renamed across files that depend on each other. Run **Tads3: Parse**
manually any time you want a full re-parse (e.g. after adding source files, changing
the makefile, or after a wide-reaching rename).

Each project folder only gets its initial full parse once per session; switching
between already-open tabs in the same project won't re-trigger it.

## Multiple makefiles

If your project contains more than one `.t3m` file, automatic makefile selection is
skipped (both on open and on save) since the plugin can't guess which one you want. Run
**Tads3: Select Makefile** to choose — the choice is remembered for the session, and
automatic re-parsing on save resumes normally afterward. To change it, run
**Tads3: Select Makefile** again followed by **Tads3: Parse**.

## LSP features

Once the server is running, all standard LSP features are available through the LSP
package's built-in UI:

| Feature | How to access |
|---------|---------------|
| Hover documentation | Hold `Ctrl` and hover, or position cursor and wait |
| Go to definition | Right-click → LSP → Go to Definition (see [Key bindings](#key-bindings) to add `F12` / click support) |
| Find references | `Shift+F12`, or right-click → LSP → Find References |
| Completions | Type to trigger automatically, or `Ctrl+Space` |
| Diagnostics | Shown inline; navigate with LSP → Next / Previous Diagnostic |
| Document symbols (outline) | `Cmd+R` (see [Key bindings](#key-bindings)), or Command Palette → **LSP: Goto Symbol** |
| Workspace symbols | `Cmd+Shift+R` (see [Key bindings](#key-bindings)), or Command Palette → **LSP: Goto Symbol in Project** |

## Key bindings

The LSP package ships most of its key bindings commented out by default — including
`F12` for Go to Definition — so they don't collide with other packages. There's also no
default mouse binding for click-to-definition. Add the ones you want to your **User**
keymap/mousemap (`Preferences → Key Bindings` / `Preferences → Mouse Bindings`, or edit
the files directly under `Packages/User/`).

**`Packages/User/Default (OSX).sublime-keymap`** — `F12` to go to definition, `Alt+F12`
for side-by-side:

```json
[
    {
        "keys": ["f12"],
        "command": "lsp_symbol_definition",
        "args": {"side_by_side": false, "force_group": true, "fallback": false, "group": -1},
        "context": [
            {"key": "lsp.session_with_capability", "operand": "definitionProvider"},
            {"key": "auto_complete_visible", "operand": false}
        ]
    },
    {
        "keys": ["alt+f12"],
        "command": "lsp_symbol_definition",
        "args": {"side_by_side": true, "force_group": true, "fallback": false, "group": -1},
        "context": [
            {"key": "lsp.session_with_capability", "operand": "definitionProvider"},
            {"key": "auto_complete_visible", "operand": false}
        ]
    },
    {
        "keys": ["super+r"],
        "command": "lsp_document_symbols",
        "context": [{"key": "lsp.session_with_capability", "operand": "documentSymbolProvider"}]
    },
    {
        "keys": ["super+shift+r"],
        "command": "lsp_workspace_symbols",
        "context": [{"key": "lsp.session_with_capability", "operand": "workspaceSymbolProvider"}]
    }
]
```

The last two bindings are context-gated: `Cmd+R` / `Cmd+Shift+R` only route to the
LSP-backed commands when the active view is a TADS3 file with an active session — in
every other file type the context evaluates false and Sublime's native "Goto Symbol" /
"Goto Symbol in Project" (which don't understand TADS3 syntax and always come up empty
for it) run as normal.

**`Packages/User/Default (OSX).sublime-mousemap`** — click to go to definition. Plain
`Ctrl+Click`, `Cmd+Click` and `Alt+Click` are already claimed by Sublime's built-in mouse
bindings on macOS (context menu, add-cursor and column-select respectively), so this uses
`Ctrl+Alt+Click` instead:

```json
[
    {
        "button": "button1",
        "count": 1,
        "modifiers": ["ctrl", "alt"],
        "press_command": "drag_select",
        "command": "lsp_symbol_definition",
        "args": {"side_by_side": false, "force_group": true, "fallback": false, "group": -1},
        "context": [
            {"key": "lsp.session_with_capability", "operand": "definitionProvider"}
        ]
    }
]
```

On Linux or Windows, use `Default (Linux).sublime-keymap` / `Default (Windows).sublime-keymap`
and the matching `.sublime-mousemap` filename instead — the JSON contents are the same,
but check for pre-existing bindings on your chosen modifiers first since the defaults
differ per platform.

## Troubleshooting

**Server does not start**
Run `Tads3: Install Server` and restart Sublime Text. Check that the binary exists and
is executable in the `Local/tads3tools/bin/` directory.

**No parse progress after opening a file**
The server needs a `.t3m` makefile in the project tree to know which files to parse.
Open Sublime Text with the project root as a folder (`File → Open Folder`), not just
a single file. If there's more than one `.t3m` candidate, automatic parsing is skipped —
run **Tads3: Select Makefile**.

**"Parse failed: No files to parse. Aborting operation"**
The server keeps parse state in globals it clears at the start of every
`request/parseDocuments` call, so two overlapping parse requests race and one of them
ends up seeing zero files. The plugin serializes all parse requests (manual and
automatic) to prevent this — if you still hit it, a parse was already running when you
triggered another one; status bar will say "A parse is already in progress" instead in
that case. Wait for the current parse to finish and try again.

**`.t` file opens with wrong syntax**
The content heuristic only checks the first line. If your file starts with a comment
before any TADS3-specific pattern, set the syntax manually via **View → Syntax → TADS3**
and save the file.
