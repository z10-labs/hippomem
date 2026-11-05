"""The fake team: config, sample repos and the memory they start with."""

from __future__ import annotations

from hippomem import config, db, decisions, explored, proposals
from hippomem.policy import Actor

TEAM_CONFIG = """
[server]
host = "127.0.0.1"
port = {port}

[acceptance]
evidence_only = "auto"
self_approval = "never"

[review]
max_per_session = 2

[[systems]]
id = "billing"
name = "Billing"
owners = ["@alex"]
repos = ["ledger-api", "ledger-worker"]

[[systems]]
id = "storefront"
name = "Storefront"
owners = ["@sam"]
repos = ["storefront"]
"""

ENGINEERS = {"@alex": "alex@example.com", "@sam": "sam@example.com"}

LEDGER_API = {
    "pyproject.toml": '[project]\nname = "ledger-api"\nversion = "0.1.0"\ndependencies = [\n    "psycopg>=3.1",\n]\n',
    "README.md": "# ledger-api\n\nOrders, refunds and payouts for the billing system.\n",
    "db/schema.sql": (
        "CREATE TABLE orders (\n    id BIGINT PRIMARY KEY,\n    customer_id BIGINT NOT NULL,\n    total_cents BIGINT NOT NULL,\n"
        "    delivered_at TIMESTAMPTZ\n);\n\nCREATE TABLE refunds (\n    id BIGINT PRIMARY KEY,\n"
        "    order_id BIGINT NOT NULL REFERENCES orders(id),\n"
        "    customer_id BIGINT NOT NULL,\n    amount_cents BIGINT NOT NULL,\n"
        "    requested_at TIMESTAMPTZ NOT NULL\n);\n"
    ),
    "src/ledger_api/__init__.py": "",
    "src/ledger_api/db.py": (
        '"""Database access. Everything goes through `connection()`."""\n\nimport os\n\nimport psycopg\n\n\n'
        'def connection():\n    return psycopg.connect(os.environ["LEDGER_DATABASE_URL"])\n'
    ),
    "src/ledger_api/orders.py": (
        "from dataclasses import dataclass\nfrom datetime import datetime\n\n\n@dataclass\nclass Order:\n    id: int\n    customer_id: int\n"
        "    total_cents: int\n    delivered_at: datetime | None = None\n\n\ndef mark_delivered(order: Order, when: datetime) -> Order:\n"
        "    return Order(order.id, order.customer_id, order.total_cents, when)\n"
    ),
    "src/ledger_api/refunds.py": (
        "from dataclasses import dataclass\nfrom datetime import datetime, timezone\n\nfrom ledger_api.orders import Order\n\n\n"
        "@dataclass\nclass Refund:\n    order_id: int\n    customer_id: int\n    amount_cents: int\n    requested_at: datetime\n\n\n"
        "class RefundError(Exception):\n    pass\n\n\ndef request_refund(order: Order, amount_cents: int) -> Refund:\n"
        '    if amount_cents <= 0 or amount_cents > order.total_cents:\n        raise RefundError("invalid amount")\n'
        "    return Refund(order.id, order.customer_id, amount_cents, datetime.now(timezone.utc))\n"
    ),
}

PAYOUTS_V1 = "BATCH_HOUR_UTC = 2\n\n\ndef next_batch(now):\n    return now.replace(hour=BATCH_HOUR_UTC, minute=0, second=0, microsecond=0)\n"
PAYOUTS_V2 = PAYOUTS_V1 + "\n\ndef batch_size():\n    return 500\n"
PAYOUTS_V3 = PAYOUTS_V2 + "\n\ndef is_batch_window(now):\n    return now.hour == BATCH_HOUR_UTC\n"

LEDGER_WORKER = {
    "pyproject.toml": '[project]\nname = "ledger-worker"\nversion = "0.1.0"\ndependencies = []\n',
    "src/ledger_worker/__init__.py": "",
    "src/ledger_worker/jobs.py": 'def run_pending():\n    """Picks up pending billing jobs."""\n    return []\n',
}

STOREFRONT = {
    "package.json": '{\n  "name": "storefront",\n  "version": "0.1.0",\n  "dependencies": {}\n}\n',
    "src/api.ts": (
        "// Calls to other services go through here.\nconst LEDGER_URL = process.env.LEDGER_URL ?? 'http://ledger-api';\n\n"
        "export async function ledger<T>(path: string): Promise<T> {\n  const res = await fetch(`${LEDGER_URL}${path}`);\n"
        "  if (!res.ok) throw new Error(`ledger ${path}: ${res.status}`);\n  return (await res.json()) as T;\n}\n"
    ),
    "src/account.ts": (
        "export type AccountPage = { customerId: number; name: string };\n\n"
        "export async function accountPage(customerId: number): Promise<AccountPage> {\n  return { customerId, name: 'Customer ' + customerId };\n}\n"
    ),
}


def seed(home_db: str) -> dict[str, str]:
    """Decisions and one explored record the team already has. Proposed by
    @sam, accepted by @alex, as if they had come through the normal flow."""
    conn = db.connect(home_db)
    policy = config.Config().policy
    alex = Actor("@alex", "engineer")
    ids: dict[str, str] = {}

    def accept(key: str, content: decisions.Content) -> None:
        p = proposals.create(
            conn, policy, kind="new", payload=decisions.content_to_payload(content), source="engineer", proposed_by="@sam", weight=content.weight
        )
        decision_id = proposals.accept(conn, policy, p.id, alex)
        assert decision_id is not None
        ids[key] = decision_id

    accept(
        "rate_limit",
        decisions.Content(
            title="Rate limiting uses Postgres advisory locks and a counts table",
            statement="Per-customer rate limits are enforced in Postgres (advisory lock + a counts table), not in a separate store.",
            why="We already run Postgres; we don't want another piece of infrastructure to operate, monitor and pay for.",
            scope="system",
            systems=("billing",),
            alternatives=(
                decisions.Alternative("Redis counters", "another service to run and monitor for one feature"),
                decisions.Alternative("In-memory counters", "break as soon as there is more than one replica"),
            ),
        ),
    )
    accept(
        "no_shared_db",
        decisions.Content(
            title="Services never read each other's databases",
            statement="A service reads another service's data only through that service's HTTP API.",
            why="Each service owns its schema. Direct reads broke the storefront twice during ledger migrations.",
            scope="team",
            weight="heavy",
            alternatives=(decisions.Alternative("Read-only DB users per consumer", "still couples consumers to the schema"),),
        ),
    )
    accept(
        "cents",
        decisions.Content(
            title="Money amounts are integer cents",
            statement="All money values are stored and passed as integer cents.",
            why="Floats lost cents in refund calculations.",
            scope="system",
            systems=("billing",),
        ),
    )
    ids["kafka"] = explored.create(
        conn,
        title="Kafka for billing events",
        tried="A 2-day spike publishing refund and payout events to Kafka",
        outcome="dropped",
        why="Running brokers cost more than it was worth at ~1k events/day, and the ordering we needed meant a single partition anyway.",
        revisit_if="Event volume passes ~100k/day, or we adopt a managed broker.",
        system_id="billing",
        repo="ledger-api",
    )
    conn.close()
    return ids
