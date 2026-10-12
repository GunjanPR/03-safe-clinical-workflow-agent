"""Copy the compact store between databases (e.g., SQLite -> PostgreSQL).

    python scripts/migrate_store.py --src sqlite:///data/clinical.db \
        --dst postgresql+psycopg://clinagent:clinagent@127.0.0.1:5433/clinagent
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clinagent.store import Base, make_engine  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="sqlite:///data/clinical.db")
    ap.add_argument("--dst", required=True)
    ap.add_argument("--batch", type=int, default=5000)
    args = ap.parse_args()
    src, dst = make_engine(args.src), make_engine(args.dst)
    tables = [t for t in Base.metadata.sorted_tables if t.name != "med_list_change"]
    Base.metadata.drop_all(dst, tables=tables)
    Base.metadata.create_all(dst)
    t0 = time.time()
    with src.connect() as s, dst.begin() as d:
        for t in tables:
            rows = s.execute(t.select()).mappings()
            n, batch = 0, []
            for row in rows:
                batch.append(dict(row))
                if len(batch) >= args.batch:
                    d.execute(t.insert(), batch)
                    n += len(batch)
                    batch = []
            if batch:
                d.execute(t.insert(), batch)
                n += len(batch)
            print(f"{t.name}: {n} rows ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
