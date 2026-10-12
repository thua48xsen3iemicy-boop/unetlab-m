"""CLI: validate a ticket, inspect its plan, or execute the lab in a container."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .model import SpecificationError, load_lab, load_ticket
from .qemu import command, consoles
from .storage import AttemptStorage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute one UNetLab laboratory attempt without UI or a database")
    parser.add_argument("action", choices=["validate", "plan", "run"])
    parser.add_argument("--ticket", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, default=Path(__file__).resolve().parents[2] / "profiles")
    parser.add_argument("--runtime", type=Path, default=Path("/run/puskalka"))
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    options = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        lab = load_lab(load_ticket(options.ticket), options.profiles)
        if options.action == "validate":
            print(json.dumps(lab.summary(), indent=2))
            return 0
        if options.action == "plan":
            storage = AttemptStorage(lab)
            result = lab.summary()
            result["consoles"] = consoles(lab)
            result["commands"] = {str(n.id): command(lab, n, storage.node_dir(n.id), options.runtime)
                                  for n in lab.nodes}
            result["note"] = "Read-only plan; startup media are prepared during run; no progress is written."
            print(json.dumps(result, indent=2))
            return 0
        from .service import LabExecutor, make_server

        executor = LabExecutor(lab, options.runtime)
        server = None
        thread = None

        def request_stop(signum, frame):
            executor.shutdown.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        try:
            executor.prepare()
            # HTTP is available while guest processes start, allowing stop/status.
            server = make_server(executor, options.listen, options.port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            executor.start_all()
            logging.info(json.dumps({"event": "lab-ready", **executor.status()}))
            previous = executor.status()
            while not executor.shutdown.wait(1):
                current = executor.status()
                if current != previous:
                    logging.info(json.dumps({"event": "lab-status", **current}))
                    previous = current
        finally:
            executor.shutdown.set()
            if server:
                server.shutdown()
                server.server_close()
            if thread:
                thread.join(timeout=2)
            executor.close()
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        logging.error(json.dumps({"event": "runner-error", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
