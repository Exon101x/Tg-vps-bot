"""Copy everything from one database to another, e.g. when you change hosting:

    python migrate.py sqlite:///bot.db postgresql://user:pass@host/dbname
    python migrate.py postgresql://... mongodb+srv://user:pass@cluster/vpsbot --force

Saved servers stay encrypted, so the new deployment must use the SAME SECRET_KEY.
Stop the bot before migrating. --force wipes the target first.
"""
import sys

from dotenv import load_dotenv

load_dotenv()

from backends import describe, open_backend  # noqa: E402


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2:
        raise SystemExit(__doc__)
    force = "--force" in argv
    src, dst = open_backend(args[0]), open_backend(args[1])
    if dst.is_populated() and not force:
        raise SystemExit(f"{describe(args[1])} already contains data. Re-run with --force to replace it.")
    data = src.dump()
    dst.load(data, replace=force)
    print(f"Copied {describe(args[0])} -> {describe(args[1])}: " + ", ".join(f"{n}={len(rows)}" for n, rows in data.items()))


if __name__ == "__main__":
    main(sys.argv[1:])
