# Krauken software

Control daemon, hardware supervisor, web API, and React frontend for the
Krauken fermentation controller. See `plans/` for the design docs and
`/Users/dneumann/.claude/plans/replicated-meandering-dragonfly.md` for the
current implementation plan and milestone breakdown.

## Local development (M0)

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Terminal 1 -- daemon
export KRAUKEN_DB_PATH=/tmp/krauken-dev.db
export KRAUKEN_DAEMON_SOCKET=/tmp/krauken-dev-daemon.sock
.venv/bin/python3 -m krauken.daemon

# Terminal 2 -- API (reads the same env vars)
export KRAUKEN_DB_PATH=/tmp/krauken-dev.db
export KRAUKEN_DAEMON_SOCKET=/tmp/krauken-dev-daemon.sock
.venv/bin/python3 -m krauken.api

# Terminal 3 -- frontend, dev server with hot reload (proxies /api to :8080)
cd frontend && npm install && npm run dev
```

For a production-shaped run (one process serving the built frontend too),
build the frontend and copy it into the API's static mount before starting
the API:

```sh
cd frontend && npm run build
cp -r dist/* ../krauken/api/_static/
```

## Tests

```sh
.venv/bin/pytest
```

## Deploying to the Pi

See `deploy/` -- three systemd units (`krauken-daemon`, `krauken-api`,
`krauken-supervisor`; the supervisor unit is drafted but not yet functional,
see its own comment), a `tmpfiles.d` rule for `/run/krauken` and
`/var/lib/krauken`, and `krauken.conf.example` for the environment file each
unit reads via `EnvironmentFile=`.
