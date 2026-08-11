# Repository Instructions

## Project Shape
- Python package `miair` plus top-level launcher `miair.py`; installed CLI entrypoint is `miair = miair.cli:main`.
- `miair.app.MiAir` wires the app: Web UI/API starts first, then DLNA and AirPlay start only when Xiaomi account/cookie and `mi_did` are configured.
- Major protocol code lives under `miair/dlna/` and `miair/airplay/`; Web API routes and settings masking live in `miair/web/api.py`.

## Commands
- Runtime requires Python `>=3.10` per `pyproject.toml`; README says Python 3.12+ for Windows usage.
- Install editable package/deps with `python3 -m pip install -e .` before importing modules or running tests; `miair.py` auto-installs runtime deps only when launched directly.
- Start locally with `python3 miair.py` or `python3 miair.py --conf-path conf --web-port 8300 --dlna-port 8200`.
- Run the existing test script with `python3 tests/test_audio_seek.py`; this is not a pytest-configured repo and `pytest` is not declared as a dependency.
- Focused pytest-style execution may still work if pytest is installed: `python3 -m pytest tests/test_audio_seek.py -k detect_audio_format`.

## Runtime And Config
- Config is loaded from `<conf-path>/config.json`; relative `--conf-path` is normalized to an absolute path in `Config.load`.
- Env fallback names are `MI_USER`, `MI_PASS`, and `MI_DID`; `MIAIR_HOSTNAME` explicitly overrides saved `hostname`, while `HOST_IP` in `.env.example` is only a commented hint and is not read by `Config`.
- Default ports are DLNA HTTP `8200` and Web UI/API `8300`; Docker exposes both and runs with host networking.
- Secrets/cookies must stay masked in API responses; preserve `_mask_cookie`, `_unmask_cookie`, and `_mask_devices` behavior when touching settings endpoints.

## Docker And Deploy
- CI only builds/pushes Docker images on `main`, `docker`, or manual dispatch; Markdown, docs, shell scripts, `.env.example`, `.gitignore`, license, and PNG changes are ignored by the workflow.
- The checked-in `Dockerfile` copies `config-example.json` and `.env.example` into the image, seeds `/app/conf/config.json` and `/app/conf/.env` if absent, then runs `python miair.py --conf-path /app/conf`.
- `deploy.sh` is interactive and root-only; it rewrites `Dockerfile` via heredoc, builds `miair:latest`, mounts a user-selected config directory to `/app/conf`, and starts container `miair` with `--network=host` and `--restart unless-stopped`.
- `manage.sh update` downloads `main.tar.gz`, rebuilds the image, removes the existing container, then re-runs `deploy.sh`; avoid assuming it is a non-interactive update path.

## Testing Quirks
- `tests/test_audio_seek.py` imports production modules and needs runtime dependencies such as `miservice-fork`, `aiohttp`, `zeroconf`, `pycryptodome`, and `av` installed.
- ffmpeg-dependent portions of the test script are best-effort: they skip or warn when ffmpeg is unavailable; pure-Python seek tests should still run once Python deps are installed.
- The current macOS system Python in this workspace is 3.9.6, below the project requirement, so use a Python 3.10+ interpreter for meaningful verification.
