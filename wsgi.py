"""Production entry point used by gunicorn inside the container."""

from carryover import create_app

app = create_app()
