"""Entry point.  Run:  python main.py   (or pythonw main.py for no console window)."""
import argparse
import sys

from assistant.app import run


def main() -> int:
    parser = argparse.ArgumentParser(description="AI PC Assistant")
    parser.add_argument("--show", action="store_true", help="show the search window at startup")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args, qt_args = parser.parse_known_args()
    return run([sys.argv[0], *qt_args], show_window=args.show, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
