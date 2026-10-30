"""python -m ta  — run the Trading Assistant."""

from .config import Settings
from .service import build_live_service, setup_logging


def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_file)
    build_live_service(settings).run()


if __name__ == "__main__":
    main()
