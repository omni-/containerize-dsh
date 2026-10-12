# Saved profiles

A profile saves the paths and settings you use for a project. Create
`containerize-dsh/profiles.json` in your user config directory, including its parent
directory if needed:

| Platform | Location |
| --- | --- |
| Windows | `%APPDATA%\containerize-dsh\profiles.json`, normally under `AppData\Roaming` |
| macOS | `~/Library/Application Support/containerize-dsh/profiles.json` |
| Linux | `$XDG_CONFIG_HOME/containerize-dsh/profiles.json`, or `~/.config/containerize-dsh/profiles.json` when unset or relative |

Keep the registry outside this public repository and out of Git. The launcher
reads it only when you select `--profile`; there's no default profile or plugin
discovery based on the current directory.

The file is a JSON object keyed by profile name. Replace these paths with your own
absolute paths; Windows paths can use forward slashes:

```json
{
  "my-project": {
    "plugins": ["/private/my-project/.dsh-plugin"],
    "repo": "/private/my-project",
    "name": "my-project-sandbox",
    "port": 11127,
    "key_file": "/private-secrets/deepseek-key"
  }
}
```

All fields are optional. The parser rejects other fields.

| Field | Meaning |
| --- | --- |
| `plugins` | Ordered list of directories following the [plugin contract](plugins.md). Selecting them trusts their code. An empty list selects no plugins. |
| `plugin` | Legacy single directory, normalized to a one-element `plugins` list. Mutually exclusive with `plugins`, even when the list is empty. |
| `repo` | Default host repository for transfers. |
| `name` | Persistent sandbox namespace for `dsh.py`. Tasks generate their own names. |
| `port` | Localhost web port, an integer from 1 to 65535. |
| `key_file` | Path to a file containing only the DeepSeek API key, never the key itself. Omit it to use environment/UI authentication. |

Paths must be absolute or start with `~`; the working directory doesn't affect
them. See [authentication](isolation.md#authentication) for key-file requirements.
Plugin entries must be nonempty strings. Duplicate paths are rejected after
expansion and resolution, including alternate spellings of the same directory.
Every selected plugin directory and its manifest paths must exist before Docker
operations begin.

## Composing capabilities

A profile can combine reusable capabilities with project-specific configuration:

```json
{
  "omnigame": {
    "plugins": [
      "/plugins/dotnet",
      "/plugins/rea",
      "/projects/omnigame/.dsh-plugin"
    ],
    "repo": "/projects/omnigame"
  }
}
```

These paths illustrate a capability stack; this repository does not ship .NET or
REA integrations. Build layers, Cordis patches, bootstrap hooks, and validation
hooks follow this exact order. Plugins without a given contribution are skipped.
Runtime directories remain separate and read-only. Environment keys and resource
limits may agree or contribute different keys; incompatible values fail with
both plugin paths and the conflicting field. Cordis IDs must be unique, and
different plugins cannot replace the same entry field with incompatible values.
See [composition rules](plugins.md#composition) for the complete contract.

Existing `"plugin": "/old/plugin"` profiles require no edit. To migrate, replace
that field with `"plugins": ["/old/plugin"]`; never leave both fields present.
This is containerize-dsh's saved-profile feature. It does not select or replace
DSH's own native profile; the container still starts `dsh web`.

## Starting tasks

```sh
dsh-task start --task fix-parser --profile my-project --open
dsh-task start --task another-fix --profile my-project --repo /another/checkout --port 11128
```

Explicit `--repo` and `--port` override saved values. Task startup takes `plugins`
and `key_file` from the profile and replaces `name` with a unique task namespace.
Task plugins must use the task's own home/workspace volumes; shared or external
volume overrides fail validation.

Task creation records the fully resolved, ordered plugin paths alongside other
startup settings. Later task commands use those records, so pass the task ID rather
than the profile name. Editing a profile won't change an existing task's saved
settings. The referenced plugin files and key file still need to be available.

## Using a profile with the low-level launcher

Global options come before the command:

```sh
python dsh.py --profile my-project build
python dsh.py --profile my-project init
python dsh.py --profile my-project up
python dsh.py --profile my-project --name another-sandbox --port 11128 up
python dsh.py --profile my-project status --repo /another/host-checkout
```

Select the profile on each invocation. Explicit CLI values override any saved
field, even when the value equals a built-in default. The singular `--plugin PATH`
option replaces the entire saved plugin list with that one selection; it remains
a singular option. A saved `repo` supplies the
default for `init`, `refresh`, `import`, and `status`. Follow the
[launcher setup sequence](launcher.md#setup) when using a new namespace.

Edit the JSON file to add, rename, or remove profiles; the launcher never writes
profiles or credentials there. Unknown profiles and invalid selected entries fail
before Docker runs. Plugin, key-file, and Compose checks still apply.
