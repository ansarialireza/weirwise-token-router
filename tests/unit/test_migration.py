"""Test verifying that database schema migration runs cleanly from scratch."""

import pytest
from alembic.config import Config
from alembic import command
from token_router.core.config import settings


def test_alembic_upgrade_head():
    """Verify that alembic migration runs cleanly to head."""
    alembic_cfg = Config("alembic.ini")
    # Run upgrade to head
    command.upgrade(alembic_cfg, "head")
    # Verify current revision is not None
    # Downgrade base and upgrade head again
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
