"""mysql CLI wrapper + assertion helpers. Uses a harness-owned client defaults file (never ~/.my.cnf)."""
import subprocess

from . import paths as P

MYSQL = P.BREW / "opt/mysql/bin/mysql"


def client_cnf(root=False):
    """This lane's client defaults file (lane A env/client*.cnf, lane B env/lanes/b/client*.cnf)."""
    P.LANE_ROOT.mkdir(parents=True, exist_ok=True)
    f = P.LANE_ROOT / ("client-root.cnf" if root else "client.cnf")
    if root:
        body = f"[client]\nuser=root\nsocket={P.RUN / 'mysql.sock'}\n"
    else:
        body = f"[client]\nuser={P.DB_USER}\npassword={P.DB_PASS}\nhost={P.HOST}\nport={P.MYSQL_PORT}\nprotocol=TCP\n"
    if not f.exists() or f.read_text() != body:
        f.write_text(body)
        f.chmod(0o600)
    return f


class SQLError(RuntimeError):
    pass


def query(sql: str, db: str | None = None, root=False, timeout=60) -> list[list[str]]:
    """Run SQL; return rows (list of string columns, header excluded). NULL comes back as 'NULL'."""
    cmd = [str(MYSQL), f"--defaults-file={client_cnf(root)}", "--batch", "--raw", "--skip-column-names"]
    if db:
        cmd.append(P.DB_NAMES.get(db, db))
    r = subprocess.run(cmd, input=sql, text=True, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise SQLError(f"mysql exit {r.returncode}: {r.stderr.strip()}  [sql: {sql.strip()[:200]}]")
    return [line.split("\t") for line in r.stdout.splitlines()]


def scalar(sql: str, db: str | None = None, **kw) -> str | None:
    rows = query(sql, db, **kw)
    return rows[0][0] if rows and rows[0] else None


OPS = {
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
}


def assert_scalar(sql: str, op: str, expected, db: str | None = None) -> dict:
    """Return an evidence dict with ok/expected/actual. Numeric compare when expected is a number."""
    actual_raw = scalar(sql, db)
    actual = actual_raw
    if isinstance(expected, (int, float)) and actual_raw not in (None, "NULL"):
        actual = type(expected)(float(actual_raw)) if isinstance(expected, int) else float(actual_raw)
    ok = actual is not None and OPS[op](actual, expected)
    return {"ok": bool(ok), "sql": sql.strip(), "db": db, "op": op, "expected": expected, "actual": actual_raw}
