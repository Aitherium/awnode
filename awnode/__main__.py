"""Allow `python -m awnode` (used by the persistent-service installers)."""

from awnode.cli import main

if __name__ == "__main__":
    main()
