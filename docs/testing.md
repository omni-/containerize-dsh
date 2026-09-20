# Testing

Run the local suite from the repository root:

```sh
python -m unittest discover -s tests -v
```

It needs Python and Git, but no Docker daemon or provider key. Tests use temporary
Git repositories and fake Docker resources to cover transfer/import boundaries,
profiles, Compose validation, task records, integration, cleanup, discard, and
installer updates. They leave active tasks alone.

## Docker smoke tests

With Docker running in Linux-container mode:

```sh
python tests/docker_smoke.py
```

This builds local core and example-plugin images, copies the example into an
external temporary directory, and uses synthetic Git repositories and fresh,
uniquely named volumes. It checks web startup, isolation, plugin hooks, snapshots,
export/import, and refresh, then removes its containers, network, and volumes.
Images remain cached. Port 11112 must be free. Its Compose override tests need
Compose 2.24.4 or newer for `!override`.

The script makes no model requests and needs no real API key. It inherits the
launching environment, so unset `DEEPSEEK_API_KEY` first to keep a real key out of
the test container.

Once `dsh-sandbox:local` is cached, run the task smoke test:

```sh
python tests/task_docker_smoke.py
python tests/task_docker_smoke.py --discard
```

The first exercises startup, browser URL retrieval, repeated review exports,
integration of the agent's diff, refusal of a dirty target, and cleanup. The
`--discard` variant checks deletion of dirty and ignored workspace files while
preserving DSH home history, then repeats discard to check that it's safe.

Both use a temporary registry, a synthetic repository, and an available localhost
port. They clear the provider key, refuse image builds, and make no model calls.
Their final teardown removes only their recorded fixture containers, networks,
and volumes, including the fixture home volume after checking its preservation.

The [plugin contract](plugins.md) describes the example and supported overrides.
