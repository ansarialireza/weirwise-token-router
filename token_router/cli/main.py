import click
import json
import httpx
from typing import Optional


@click.group()
@click.option("--url", default="http://localhost:8000", help="Token Router Base URL")
@click.option("--api-key", default=None, help="Consumer or Admin API Key")
@click.pass_context
def cli(ctx, url: str, api_key: Optional[str]):
    """wtr - Official CLI for Weirwise Token Router (Universal Service Integration Platform)."""
    ctx.ensure_object(dict)
    ctx.obj["URL"] = url.rstrip("/")
    ctx.obj["API_KEY"] = api_key


@cli.command()
@click.pass_context
def status(ctx):
    """Check Token Router platform health and reachability."""
    url = f"{ctx.obj['URL']}/health"
    try:
        resp = httpx.get(url, timeout=5.0)
        click.echo(f"[{resp.status_code}] {resp.text}")
    except Exception as e:
        click.echo(f"Error connecting to {url}: {e}", err=True)


@cli.command()
@click.option("--category", default=None, help="Optional category filter (ai, communication, payments, storage, data)")
@click.pass_context
def discover(ctx, category: Optional[str]):
    """Discover all supported service categories, providers, operations, and models."""
    url = f"{ctx.obj['URL']}/api/v1/discover"
    params = {"category": category} if category else {}
    headers = {"Authorization": f"Bearer {ctx.obj['API_KEY']}"} if ctx.obj["API_KEY"] else {}
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=10.0)
        click.echo(json.dumps(resp.json(), indent=2))
    except Exception as e:
        click.echo(f"Discovery error: {e}", err=True)


@cli.command()
@click.pass_context
def diagnostics(ctx):
    """Run automated system self-diagnostics across database, vault, and categories."""
    url = f"{ctx.obj['URL']}/api/v1/diagnostics"
    try:
        resp = httpx.get(url, timeout=10.0)
        click.echo(json.dumps(resp.json(), indent=2))
    except Exception as e:
        click.echo(f"Diagnostics error: {e}", err=True)


@cli.command()
@click.argument("message")
@click.option("--provider", default=None, help="Provider e.g. openai, anthropic")
@click.option("--model", default="auto", help="Model name or auto")
@click.pass_context
def chat(ctx, message: str, provider: Optional[str], model: str):
    """Send an interactive chat completion request."""
    if not ctx.obj["API_KEY"]:
        click.echo("Error: --api-key is required to execute requests.", err=True)
        return

    url = f"{ctx.obj['URL']}/api/v1/route"
    headers = {
        "Authorization": f"Bearer {ctx.obj['API_KEY']}",
        "Content-Type": "application/json",
    }
    payload = {
        "capability": "chat",
        "provider": provider,
        "model": model,
        "request": {
            "messages": [{"role": "user", "content": message}]
        },
    }
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=30.0)
        click.echo(f"Response [{resp.status_code}]:")
        click.echo(json.dumps(resp.json(), indent=2))
    except Exception as e:
        click.echo(f"Routing request error: {e}", err=True)


if __name__ == "__main__":
    cli()
