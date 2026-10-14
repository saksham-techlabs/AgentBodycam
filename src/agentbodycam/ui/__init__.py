"""Packaged Replay UI.

``static/`` holds the production build of the React/TypeScript frontend in ``web/``
(``npm run build`` writes it here). It ships inside the Python package, so running the
Replay UI needs no Node.js; Node is a development and build-time tool only. The files are
served by :mod:`agentbodycam.core.replay_server` through :mod:`agentbodycam.core.ui_assets`.
"""
