"""Repo Analysis Tool (RAT) - backend package.

Modules:
  db       - SQLite storage layer and schema
  ingest   - repository ingestion (zip upload / clone URL) and git-log parser
  metrics  - metric computation queries (file, directory, repository, commit
             set and author metrics)
  app      - Flask application exposing the REST API and the dashboard
"""

__version__ = "1.0.0"
