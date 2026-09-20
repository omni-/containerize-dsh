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
    "plugin": "/private/my-project/.dsh-plugin",
    "repo": "/private/my-project",
    "name": "my-project-sandbox",
    "port": 11127,
    "key_file": "/private-secrets/deepseek-key"
  }
}
```

All five fields are optional. The parser rejects other fields.

| Field | Meaning |
| --- | --- |
| `plugin` | Directory following the [plugin contract](plugins.md). Selecting it trusts its code. |
| `repo` | Default host repository for transfers. |
| `name` | Persistent sandbox namespace for `dsh.py`. Tasks generate their own names. |
| `port` | Localhost web port, an integer from 1 to 65535. |
| `key_file` | Path to a file containing only the DeepSeek API key, never the key itself. Omit it to use environment/UI authentication. |

Paths must be absolute or start with `~`; the working directory doesn't affect
them. See [authentication](isolation.md#authentication) for key-file requirements.

## Starting tasks

```sh
dsh-task start --task fix-parser --profile my-project --open
dsh-task start --task another-fix --profile my-project --repo /another/checkout --port 11128
```

Explicit `--repo` and `--port` override saved values. Task startup takes `plugin`
and `key_file` from the profile and replaces `name` with a unique task namespace.
Task plugins must use the task's own home/workspace volumes; shared or external
volume overrides fail validation.

Later task commands use settings recorded at startup, so pass the task ID rather
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
field, even when the value equals a built-in default. A saved `repo` supplies the
default for `init`, `refresh`, `import`, and `status`. Follow the
[launcher setup sequence](launcher.md#setup) when using a new namespace.

Edit the JSON file to add, rename, or remove profiles; the launcher never writes
profiles or credentials there. Unknown profiles and invalid selected entries fail
before Docker runs. Plugin, key-file, and Compose checks still apply.
