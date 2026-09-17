"""Missions: multi-platform 3D scenes described by one JSON file. Self-contained, see README.md.

The rest of the app touches this package in two places only: `missions.attach(app)` in app.py and the
`missions_shell.js` script tag in static/index.html. Remove those and it's gone.
"""
from .routes import attach

__all__ = ["attach"]
