import logging
import os
import subprocess
import sys
from datetime import date

from trading_calendar import is_trading_day


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdio()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True,
    handlers=[logging.StreamHandler(sys.stdout)],
)


def main() -> None:
    today = date.today()
    if not is_trading_day(today):
        logging.info("today is not a trading day, skip scheduled run")
        raise SystemExit(0)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    cli_path = os.path.join(script_dir, "pipeline_cli.py")
    cmd = [sys.executable, cli_path, "run"]

    logging.info("launching pipeline command: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, cwd=script_dir)
    except Exception as exc:
        logging.error("failed to launch pipeline cli: %s", exc)
        raise SystemExit(1) from exc

    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
