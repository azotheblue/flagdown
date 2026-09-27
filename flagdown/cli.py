"""Flagdown CLI - one command, not a pile of scripts."""

from __future__ import annotations

import sys
from collections import Counter
from importlib import resources
from types import SimpleNamespace

import click
from dotenv import load_dotenv

load_dotenv()


def _content_options(fn):
    fn = click.option(
        "--path",
        "content_path",
        default=".",
        show_default=True,
        type=click.Path(file_okay=False, dir_okay=True, path_type=str),
        help="Content root (folders from categories.yaml)",
    )(fn)
    fn = click.option(
        "--config",
        default=None,
        type=click.Path(exists=True, dir_okay=False, path_type=str),
        help="Path to categories.yaml",
    )(fn)
    fn = click.option(
        "--category",
        default=None,
        help="Filter by category (partial match)",
    )(fn)
    return fn


@click.group()
@click.version_option(package_name="flagdown")
def cli():
    """Flagdown - Markdown to CTFd for course / training CTFs."""


@cli.command("list")
@_content_options
def list_cmd(content_path, config, category):
    """Discover and list challenge files (no upload)."""
    from flagdown.core import run_sync

    raise SystemExit(
        run_sync(
            SimpleNamespace(
                path=content_path,
                config=config,
                category=category,
                list=True,
                dry_run=False,
                all=False,
                file=None,
                update=False,
                add_requirements=False,
                fresh=False,
                yes=False,
                probe_fallback=False,
                skip_existing=True,
            )
        )
    )


@cli.command("dry-run")
@_content_options
def dry_run_cmd(content_path, config, category):
    """Parse challenges and preview what would upload."""
    from flagdown.core import run_sync

    raise SystemExit(
        run_sync(
            SimpleNamespace(
                path=content_path,
                config=config,
                category=category,
                list=False,
                dry_run=True,
                all=False,
                file=None,
                update=False,
                add_requirements=False,
                fresh=False,
                yes=False,
                probe_fallback=False,
                skip_existing=True,
            )
        )
    )


@cli.command("sync")
@_content_options
@click.option("--all", "sync_all", is_flag=True, help="Upload all categories")
@click.option("--file", "file_path", default=None, help="Upload a single markdown file")
@click.option("--update", is_flag=True, help="Update existing challenges (fields, flags, hints, files, solutions)")
@click.option(
    "--add-requirements",
    is_flag=True,
    help="Gate non-intro challenges behind each category's intro (0) challenge",
)
@click.option("--fresh", is_flag=True, help="Delete matching challenges before upload")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation (with --fresh)")
@click.option(
    "--probe-fallback",
    is_flag=True,
    help="Probe challenge IDs when admin list endpoint returns 500",
)
def sync_cmd(
    content_path,
    config,
    category,
    sync_all,
    file_path,
    update,
    add_requirements,
    fresh,
    yes,
    probe_fallback,
):
    """Upload markdown challenges to CTFd."""
    from flagdown.core import run_sync

    if not sync_all and not category and not file_path:
        raise click.UsageError("Specify --all, --category, or --file")

    raise SystemExit(
        run_sync(
            SimpleNamespace(
                path=content_path,
                config=config,
                category=category,
                list=False,
                dry_run=False,
                all=sync_all,
                file=file_path,
                update=update,
                add_requirements=add_requirements,
                fresh=fresh,
                yes=yes,
                probe_fallback=probe_fallback,
                skip_existing=True,
            )
        )
    )


@cli.command("status")
@click.option("--probe-fallback", is_flag=True, help="Probe IDs when admin list returns 500")
def status_cmd(probe_fallback):
    """Show live challenge counts from CTFd."""
    from flagdown.core import CTFD_TOKEN, CTFD_URL, CTFdClient

    if not CTFD_TOKEN:
        click.echo("[ERROR] CTFD_TOKEN not set. Create a .env file with your API token.")
        raise SystemExit(1)

    click.echo(f"CTFd: {CTFD_URL}")
    client = CTFdClient(CTFD_URL, CTFD_TOKEN, probe_fallback=probe_fallback)
    if not client.test_connection():
        raise SystemExit(1)

    click.echo("Fetching challenges...")
    challenges = client.hydrate_challenge_details(client.get_challenges(use_cache=False))

    by_category = Counter(ch.get("category", "Uncategorized") for ch in challenges)
    by_type = Counter(ch.get("type", "unknown") for ch in challenges)
    by_state = Counter(ch.get("state") for ch in challenges if ch.get("state"))
    total_points = sum(ch.get("value", 0) or 0 for ch in challenges)

    click.echo()
    click.echo(f"Total challenges:  {len(challenges)}")
    if by_state:
        click.echo(f"  Visible:         {by_state.get('visible', 0)}")
        click.echo(f"  Hidden:          {by_state.get('hidden', 0)}")
    click.echo(f"Total point value: {total_points}")
    click.echo()
    click.echo("By type:")
    for ch_type, count in sorted(by_type.items()):
        click.echo(f"  {ch_type:<22} {count}")
    click.echo()
    click.echo("By category:")
    for cat, count in sorted(by_category.items()):
        click.echo(f"  {cat:<24} {count}")


@cli.command("hide")
@click.option("--category", default=None, help="Only hide challenges in matching categories")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def hide_cmd(category, yes):
    """Set challenges to hidden on CTFd."""
    from flagdown.core import CTFD_TOKEN, CTFD_URL, CTFdClient

    if not CTFD_TOKEN:
        click.echo("[ERROR] CTFD_TOKEN not set.")
        raise SystemExit(1)

    client = CTFdClient(CTFD_URL, CTFD_TOKEN)
    if not client.test_connection():
        raise SystemExit(1)

    challenges = client.hydrate_challenge_details(client.get_challenges(use_cache=False))
    if category:
        challenges = [
            c for c in challenges if category.lower() in (c.get("category") or "").lower()
        ]

    if not challenges:
        click.echo("No challenges matched.")
        return

    if not yes and not click.confirm(f"Hide {len(challenges)} challenge(s)?"):
        click.echo("Aborted.")
        return

    hidden = 0
    already = 0
    for ch in challenges:
        if ch.get("state") == "hidden":
            already += 1
            continue
        client.update_challenge_fields(ch["id"], {"state": "hidden"})
        click.echo(f"  [HIDDEN] {ch.get('name')}")
        hidden += 1

    click.echo(f"\nHidden: {hidden}, already hidden: {already}")


@cli.command("delete")
@click.option("--category", default=None, help="Only delete challenges in matching categories")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.option("--probe-fallback", is_flag=True)
def delete_cmd(category, yes, probe_fallback):
    """Delete challenges from CTFd (destructive)."""
    from flagdown.core import CTFD_TOKEN, CTFD_URL, CTFdClient

    if not CTFD_TOKEN:
        click.echo("[ERROR] CTFD_TOKEN not set.")
        raise SystemExit(1)

    client = CTFdClient(CTFD_URL, CTFD_TOKEN, probe_fallback=probe_fallback)
    if not client.test_connection():
        raise SystemExit(1)

    challenges = client.get_challenges(use_cache=False)
    if category:
        challenges = [
            c for c in challenges if category.lower() in (c.get("category") or "").lower()
        ]
        phrase = "DELETE"
        label = f"category matching '{category}'"
    else:
        phrase = "DELETE ALL"
        label = "ALL categories"

    if not challenges:
        click.echo("No challenges matched.")
        return

    click.echo(f"About to delete {len(challenges)} challenge(s) ({label})")
    if not yes:
        typed = click.prompt(f"Type '{phrase}' to confirm", default="", show_default=False)
        if typed != phrase:
            click.echo("Aborted.")
            return

    deleted, errors = client.delete_all_challenges(category_filter=category)
    click.echo(f"Deleted: {deleted}, errors: {errors}")
    raise SystemExit(0 if errors == 0 else 1)


@cli.command("fix-hints")
@click.option("--category", default=None, help="Only challenges whose category contains this")
@click.option("--dry-run", is_flag=True, help="Preview without patching")
def fix_hints_cmd(category, dry_run):
    """Re-apply sequential hint unlock + scaled costs on live CTFd."""
    argv = []
    if category:
        argv.extend(["--category", category])
    if dry_run:
        argv.append("--dry-run")

    from flagdown.fix_hints import main as fix_hints_main

    saved = sys.argv
    try:
        sys.argv = ["flagdown fix-hints", *argv]
        raise SystemExit(int(fix_hints_main() or 0))
    finally:
        sys.argv = saved


@cli.group("ui")
def ui_group():
    """Paste-ready CTFd UI snippets (Theme footer / Settings Editor)."""


@ui_group.command("list")
def ui_list_cmd():
    """List available UI snippets."""
    click.echo("Available UI snippets:\n")
    click.echo("  track-nav       Sticky category jump bar on /challenges")
    click.echo("  sort-by-name    Natural name order (core-beta Settings Editor)")
    click.echo("\nPrint one with:  flagdown ui track-nav | flagdown ui sort-by-name")


def _print_ui_resource(name: str) -> None:
    package = "flagdown.ui"
    with resources.files(package).joinpath(name).open("r", encoding="utf-8") as f:
        click.echo(f.read(), nl=False)


@ui_group.command("track-nav")
def ui_track_nav_cmd():
    """Print sticky track/category nav HTML+JS (paste into Theme Footer)."""
    _print_ui_resource("track_nav.html")


@ui_group.command("sort-by-name")
def ui_sort_by_name_cmd():
    """Print natural name-sort JS (paste into core-beta Settings Editor)."""
    _print_ui_resource("sort_by_name.js")


if __name__ == "__main__":
    cli()
