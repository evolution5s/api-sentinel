"""Flask HTTP wrapper around the api-sentinel crew job.

Keeps the existing cron schedule (see railway.json) as the primary
automatic trigger, while additionally exposing:

  - GET  /health   -> liveness/health check, never runs the crew
  - POST /trigger  -> runs the crew job once, synchronously

The crew logic itself (crew.py) is unchanged; run_crew() is imported and
invoked lazily, only when /trigger is actually hit, so importing this
module (and starting the server) never kicks off a crew run on its own.
"""

import logging
import os
from datetime import datetime, timezone

from flask import Flask, jsonify

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200


@app.route("/trigger", methods=["POST"])
def trigger():
    try:
        logger.info(f"Crew trigger received at {datetime.now(timezone.utc).isoformat()}")
        # Imported here (not at module load) so the crew's own module-level
        # setup (agent/LLM construction, reading state files, etc.) only
        # happens when a trigger actually fires, not at server startup.
        from crew import run_crew

        run_crew()
        return jsonify({"status": "success", "message": "Crew job completed"}), 200
    except Exception as e:
        logger.exception("Crew job failed")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
