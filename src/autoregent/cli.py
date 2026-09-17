from pathlib import Path

import typer
import uvicorn

app = typer.Typer(
    name="autoregent",
    help="Autoregent -- an API gateway that heals failing upstreams but is structurally incapable of doing it silently.",
    no_args_is_help=True,
)

_APP_TEMPLATE = '''"""Starter Autoregent config. Point `autoregent serve autoregent_app:app` at
this file, or import `gateway`/`app` directly into your own project.

Swap AccountBalance for your own upstream's expected response schema, and
adjust the route rules below to match your own paths."""

from autoregent import Autoregent, AutoregentConfig, RouteRules
from autoregent.demo import AccountBalance  # replace with your own schema(s)

config = AutoregentConfig.from_env()  # reads GEMINI_API_KEY etc. from .env

# Gemini is the default diagnoser, inferred from GEMINI_API_KEY. To use a
# different provider instead, install the extra and pass it explicitly:
#   from autoregent.diagnosers import OpenAIDiagnoser, AnthropicDiagnoser
#   diagnoser = OpenAIDiagnoser(api_key="sk-...")
# ...then add diagnoser=diagnoser to the Autoregent(...) call below.

rules = (
    RouteRules()
    # Write paths -- transfers, charges, ledger entries -- are never healed,
    # by design. Add your own patterns here.
    .transactional("*/transfer/*", "*/charge/*", "*/payments/*")
    # Informational routes eligible for AI-diagnosed healing need a registered
    # expected schema. Unregistered paths are proxied but never healed.
    .expect("accounts/*", AccountBalance)
)

gateway = Autoregent(config=config, rules=rules)
app = gateway.app
'''

_ENV_TEMPLATE = """UPSTREAM_BASE_URL=http://localhost:9000
UPSTREAM_TIMEOUT_SECONDS=5.0
GEMINI_API_KEY=
GEMINI_MODEL=gemini-flash-lite-latest
GEMINI_TIMEOUT_SECONDS=3.0
CONFIDENCE_THRESHOLD=0.85
LOG_LEVEL=INFO
# Change this for any real deployment -- proves integrity, not non-repudiation.
HMAC_SECRET=change-me-in-production
"""


@app.command()
def init(
    directory: Path = typer.Argument(Path("."), help="Where to write autoregent_app.py and .env.example"),
) -> None:
    """Scaffold a starter config file and .env.example in DIRECTORY."""
    directory.mkdir(parents=True, exist_ok=True)
    app_file = directory / "autoregent_app.py"
    env_file = directory / ".env.example"

    for path, content in [(app_file, _APP_TEMPLATE), (env_file, _ENV_TEMPLATE)]:
        if path.exists():
            typer.secho(f"skipped {path} (already exists)", fg=typer.colors.YELLOW)
            continue
        path.write_text(content)
        typer.secho(f"wrote {path}", fg=typer.colors.GREEN)

    typer.echo(
        "\nNext steps:\n"
        "  1. cp .env.example .env, fill in GEMINI_API_KEY and your own UPSTREAM_BASE_URL\n"
        "  2. edit autoregent_app.py -- swap in your own route rules and schema(s)\n"
        "  3. autoregent serve autoregent_app:app"
    )


@app.command()
def serve(
    target: str = typer.Argument(..., help="Import path to an ASGI app, e.g. autoregent_app:app (uvicorn-style)"),
    host: str = typer.Option("0.0.0.0", help="Bind address"),
    port: int = typer.Option(8000, help="Bind port"),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (development only)"),
) -> None:
    """Run an Autoregent app (or any ASGI app) with uvicorn."""
    uvicorn.run(target, host=host, port=port, reload=reload)


@app.command()
def demo(
    host: str = typer.Option("0.0.0.0", help="Bind address"),
    port: int = typer.Option(8000, help="Bind port"),
) -> None:
    """Run the built-in demo -- the gateway and a flaky mock upstream in one
    process, with zero config. Reads GEMINI_API_KEY from the environment if
    present; without it, drift detection still runs but every heal fails
    loud (never heal blind), which is itself worth seeing."""
    import os

    from .demo import build_demo_app

    demo_app = build_demo_app(gemini_api_key=os.environ.get("GEMINI_API_KEY"), port=port)
    uvicorn.run(demo_app, host=host, port=port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
