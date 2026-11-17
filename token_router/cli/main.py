"""Command Line Interface for Weirwise Token Router (wtr)."""

import asyncio
import json
import os
import sys
import click
import httpx


@click.group()
@click.option("--url", envvar="TOKEN_ROUTER_URL", default="http://localhost:8080", help="Token Router API Base URL")
@click.option("--key", envvar="TOKEN_ROUTER_KEY", default="", help="Consumer or Admin API Key")
@click.pass_context
def cli(ctx, url, key):
    """Weirwise Token Router Developer & Agent CLI."""
    ctx.ensure_object(dict)
    ctx.obj["URL"] = url.rstrip("/")
    ctx.obj["KEY"] = key


@cli.command()
@click.pass_context
def status(ctx):
    """Check Token Router health and readiness."""
    url = ctx.obj["URL"]
    try:
        resp = httpx.get(f"{url}/health/ready", timeout=5.0)
        click.echo(json.dumps(resp.json(), indent=2))
    except Exception as e:
        click.echo(f"Error connecting to Token Router at {url}: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.pass_context
def discover(ctx):
    """Discover available AI providers, models, and capabilities."""
    url = ctx.obj["URL"]
    key = ctx.obj["KEY"]
    headers = {"X-Consumer-Key": key} if key else {}
    try:
        resp = httpx.get(f"{url}/api/v1/discover", headers=headers, timeout=10.0)
        click.echo(json.dumps(resp.json(), indent=2))
    except Exception as e:
        click.echo(f"Discovery failed: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.pass_context
def diagnostics(ctx):
    """Run infrastructure and database self-diagnostics."""
    url = ctx.obj["URL"]
    try:
        resp = httpx.get(f"{url}/api/v1/diagnostics", timeout=10.0)
        click.echo(json.dumps(resp.json(), indent=2))
    except Exception as e:
        click.echo(f"Diagnostics error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("message")
@click.option("--provider", default=None, help="Provider e.g. openai, anthropic")
@click.option("--model", default="auto", help="Model name or 'auto'")
@click.pass_context
def chat(ctx, message, provider, model):
    """Execute a test routed chat query."""
    url = ctx.obj["URL"]
    key = ctx.obj["KEY"]
    if not key:
        click.echo("Error: A valid --key (or TOKEN_ROUTER_KEY env) is required.", err=True)
        sys.exit(1)

    payload = {
        "provider": provider,
        "capability": "chat",
        "model": model,
        "request": {"messages": [{"role": "user", "content": message}]},
    }
    try:
        resp = httpx.post(f"{url}/api/v1/route", headers={"X-Consumer-Key": key}, json=payload, timeout=30.0)
        click.echo(json.dumps(resp.json(), indent=2))
    except Exception as e:
        click.echo(f"Chat route failed: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli(obj={})
