"""Meridian's starting state: the team config and the five repos as they are
on 2026-01-05, before any recorded decision.

Two files carry reasons only their authors know (timeline.toml, [[legacy]]):
ledger-worker's src/payouts/rounding.py (Jo) and storefront's
src/lib/cookies.ts (Sam). They are committed by those engineers in 2025, with
no comment saying why, so the only way to learn the reason is to ask.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

HERE = Path(__file__).parent

TEAM_CONFIG = """
[server]
host = "127.0.0.1"
port = {port}

[acceptance]
evidence_only = "auto"
self_approval = "never"

[review]
max_per_session = 2

[branches]
stale_after_days = 30

[[systems]]
id = "billing"
name = "Billing"
owners = ["@alex"]
repos = ["ledger-api", "ledger-worker:src/ledger_worker/**"]

[[systems]]
id = "payouts"
name = "Payouts"
owners = ["@jo"]
repos = ["ledger-worker:src/payouts/**"]

[[systems]]
id = "storefront"
name = "Storefront"
owners = ["@sam"]
depends_on = ["ui-kit", "billing"]
repos = ["storefront"]

[[systems]]
id = "ui-kit"
name = "UI kit"
owners = ["@sam"]
repos = ["ui-kit"]

[[systems]]
id = "platform"
name = "Platform"
owners = ["@priya"]
repos = ["infra"]
"""

LEDGER_API = {
    "README.md": "# ledger-api\n\nMeridian's ledger: accounts, entries, invoices. Python, Starlette, Postgres.\n",
    "requirements.txt": "starlette==0.41.3\nuvicorn==0.32.0\npsycopg[binary]==3.2.3\n",
    "src/ledger_api/__init__.py": "",
    "src/ledger_api/app.py": '''"""HTTP routes."""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ledger_api import entries, invoices


async def create_entry(request: Request) -> JSONResponse:
    body = await request.json()
    entry = entries.create(body["account_id"], body["amount"], body["currency"], body.get("memo", ""))
    return JSONResponse(entry, status_code=201)


async def list_entries(request: Request) -> JSONResponse:
    return JSONResponse(entries.for_account(request.path_params["account_id"]))


async def create_invoice(request: Request) -> JSONResponse:
    body = await request.json()
    return JSONResponse(invoices.create(body["account_id"], body["lines"], body.get("terms", "net30")), status_code=201)


app = Starlette(
    routes=[
        Route("/v1/entries", create_entry, methods=["POST"]),
        Route("/v1/accounts/{account_id}/entries", list_entries),
        Route("/v1/invoices", create_invoice, methods=["POST"]),
    ]
)
''',
    "src/ledger_api/db.py": '''"""Postgres access."""

import os

import psycopg

DSN = os.environ.get("LEDGER_DATABASE_URL", "postgresql://localhost/ledger")


def connect() -> psycopg.Connection:
    return psycopg.connect(DSN)
''',
    "src/ledger_api/entries.py": '''"""Ledger entries."""

from ledger_api import db
from ledger_api.helpers import new_id


def create(account_id: str, amount: float, currency: str, memo: str = "") -> dict:
    entry_id = new_id("ent")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO entries (id, account_id, amount, currency, memo) VALUES (%s, %s, %s, %s, %s)",
            (entry_id, account_id, amount, currency, memo),
        )
    return {"id": entry_id, "account_id": account_id, "amount": amount, "currency": currency, "memo": memo}


def for_account(account_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT id, amount, currency, memo FROM entries WHERE account_id = %s ORDER BY created_at", (account_id,)).fetchall()
    return [{"id": r[0], "amount": r[1], "currency": r[2], "memo": r[3]} for r in rows]
''',
    "src/ledger_api/invoices.py": '''"""Invoices."""

from ledger_api import db
from ledger_api.helpers import new_id


def create(account_id: str, lines: list[dict], terms: str = "net30") -> dict:
    invoice_id = new_id("inv")
    total = sum(line["amount"] for line in lines)
    with db.connect() as conn:
        conn.execute("INSERT INTO invoices (id, account_id, total, terms) VALUES (%s, %s, %s, %s)", (invoice_id, account_id, total, terms))
    return {"id": invoice_id, "account_id": account_id, "total": total, "terms": terms}
''',
    "src/ledger_api/helpers.py": '''"""Small helpres used across the servcie."""

import secrets


def new_id(prefix: str) -> str:
    """Retrun a new random id with a type prefix, like ent_3f9a..."""
    return f"{prefix}_{secrets.token_hex(8)}"
''',
    "migrations/001_initial.sql": """CREATE TABLE entries (
    id text PRIMARY KEY,
    account_id text NOT NULL,
    amount double precision NOT NULL,
    currency text NOT NULL,
    memo text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE invoices (
    id text PRIMARY KEY,
    account_id text NOT NULL,
    total double precision NOT NULL,
    terms text NOT NULL DEFAULT 'net30',
    created_at timestamptz NOT NULL DEFAULT now()
);
""",
    "tests/test_helpers.py": """from ledger_api.helpers import new_id


def test_new_id_has_prefix():
    assert new_id("ent").startswith("ent_")
""",
}

LEDGER_WORKER = {
    "README.md": "# ledger-worker\n\nBackground jobs for billing and payouts: the scheduler, payout batches, the Paystream client.\n",
    "requirements.txt": "httpx==0.27.0\npsycopg[binary]==3.2.3\n",
    "src/ledger_worker/__init__.py": "",
    "src/ledger_worker/scheduler.py": '''"""A small in-process scheduler: run registered jobs at fixed UTC times."""

import time
from datetime import datetime, timezone
from typing import Callable

JOBS: list[tuple[str, Callable[[], None]]] = []


def every_day_at(hhmm: str):
    def register(fn: Callable[[], None]) -> Callable[[], None]:
        JOBS.append((hhmm, fn))
        return fn

    return register


def run_forever() -> None:
    while True:
        now = datetime.now(timezone.utc).strftime("%H:%M")
        for hhmm, fn in JOBS:
            if hhmm == now:
                fn()
        time.sleep(60)
''',
    "src/ledger_worker/jobs.py": '''"""Billing jobs."""

from ledger_worker.scheduler import every_day_at


@every_day_at("01:00")
def close_daily_books() -> None:
    tmp = []
    for account_id in _accounts_with_activity():
        tmp.append(account_id)
    print(f"closed books for {len(tmp)} accounts")


def _accounts_with_activity() -> list[str]:
    return []
''',
    "src/ledger_worker/payout_batch.py": '''"""Builds the day's payouts. Not scheduled yet."""

from payouts import client


def run() -> None:
    for payout in pending_payouts():
        client.send(payout)


def pending_payouts() -> list[dict]:
    return []
''',
    "src/payouts/__init__.py": "",
    "src/payouts/client.py": '''"""Paystream API client."""

import os

import httpx

BASE_URL = os.environ.get("PAYSTREAM_URL", "https://api.paystream.example")


def send(payout: dict) -> dict:
    """Send one payout to its recipent."""
    response = httpx.post(f"{BASE_URL}/v2/payouts", json=payout, headers={"Authorization": f"Bearer {os.environ['PAYSTREAM_KEY']}"})
    response.raise_for_status()
    return response.json()
''',
}

# Committed separately by Jo, in 2025 (see the module docstring).
PAYOUT_ROUNDING = '''"""Rounding payout amounts to minor units."""

from decimal import ROUND_HALF_EVEN, Decimal


def to_minor_units(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
'''

STOREFRONT = {
    "README.md": "# storefront\n\nMeridian's customer-facing shop and checkout. TypeScript, React, server rendering.\n",
    "package.json": '{\n  "name": "storefront",\n  "private": true,\n  "dependencies": {\n    "react": "18.3.1",\n    "react-dom": "18.3.1"\n  }\n}\n',
    "src/lib/api.ts": """const LEDGER_URL = process.env.LEDGER_URL ?? "http://ledger-api:8000";

export async function getEntries(accountId: string) {
  const res = await fetch(`${LEDGER_URL}/v1/accounts/${accountId}/entries`);
  return res.json();
}

export async function createEntry(accountId: string, amount: number, currency: string) {
  const res = await fetch(`${LEDGER_URL}/v1/entries`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ account_id: accountId, amount, currency }),
  });
  return res.json();
}
""",
    "src/pages/checkout.tsx": """import { useEffect, useState } from "react";
import { getEntries } from "../lib/api";

export default function Checkout({ accountId }: { accountId: string }) {
  const [entries, setEntries] = useState<any[]>([]);
  useEffect(() => {
    getEntries(accountId).then(setEntries);
  }, [accountId]);
  const total = entries.reduce((sum, e) => sum + e.amount, 0);
  return (
    <main>
      <h1>Chekout</h1>
      <p>Total: {total.toFixed(2)}</p>
      <button>Pay</button>
    </main>
  );
}
""",
    "src/pages/account.tsx": """export default function Account({ name }: { name: string }) {
  return (
    <main>
      <h1>Your account</h1>
      <p>Signed in as {name}</p>
    </main>
  );
}
""",
}

# Committed separately by Sam, in 2025 (see the module docstring).
SESSION_COOKIES = """export function sessionCookie(token: string): string {
  return `meridian_session=${token}; Path=/; HttpOnly; Secure; SameSite=None`;
}
"""

UI_KIT = {
    "README.md": "# ui-kit\n\nShared React components for Meridian's apps.\n",
    "package.json": '{\n  "name": "ui-kit",\n  "private": true,\n  "peerDependencies": {\n    "react": "^18.3.0"\n  }\n}\n',
    "src/index.ts": 'export { Button } from "./Button";\n',
    "src/Button.tsx": """import type { ReactNode } from "react";

export function Button({ children, onClick }: { children: ReactNode; onClick?: () => void }) {
  return (
    <button onClick={onClick} style={{ padding: "8px 16px", borderRadius: 4 }}>
      {children}
    </button>
  );
}
""",
}

INFRA = {
    "README.md": "# infra\n\nHow Meridian's services run: compose files for local and staging, the API gateway config.\n\n## Datastores\n\nPostgres 16.\n",
    "docker-compose.yml": """services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: dev
    volumes:
      - pgdata:/var/lib/postgresql/data
  ledger-api:
    build: ../ledger-api
    environment:
      LEDGER_DATABASE_URL: postgresql://postgres:dev@postgres/ledger
  ledger-worker:
    build: ../ledger-worker
  storefront:
    build: ../storefront
  gateway:
    image: envoyproxy/envoy:v1.31-latest
    volumes:
      - ./gateway/envoy.yaml:/etc/envoy/envoy.yaml
    ports:
      - "8080:8080"
volumes:
  pgdata: {}
""",
    "gateway/envoy.yaml": """static_resources:
  listeners:
    - name: public
      address: { socket_address: { address: 0.0.0.0, port_value: 8080 } }
      filter_chains:
        - filters:
            - name: envoy.filters.network.http_connection_manager
              typed_config:
                "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
                stat_prefix: public
                route_config:
                  virtual_hosts:
                    - name: all
                      domains: ["*"]
                      routes:
                        - match: { prefix: "/v1/" }
                          route: { cluster: ledger-api }
                        - match: { prefix: "/" }
                          route: { cluster: storefront }
                http_filters:
                  - name: envoy.filters.http.router
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
  clusters:
    - name: ledger-api
      type: STRICT_DNS
      load_assignment:
        cluster_name: ledger-api
        endpoints: [{ lb_endpoints: [{ endpoint: { address: { socket_address: { address: ledger-api, port_value: 8000 } } } }] }]
    - name: storefront
      type: STRICT_DNS
      load_assignment:
        cluster_name: storefront
        endpoints: [{ lb_endpoints: [{ endpoint: { address: { socket_address: { address: storefront, port_value: 3000 } } } }] }]
""",
}

REPOS = {
    "ledger-api": ("@alex", LEDGER_API),
    "ledger-worker": ("@alex", LEDGER_WORKER),
    "storefront": ("@sam", STOREFRONT),
    "ui-kit": ("@sam", UI_KIT),
    "infra": ("@priya", INFRA),
}

LEGACY_COMMITS = [
    ("ledger-worker", "@jo", "2025-06-10T14:00:00Z", "payouts: round to minor units", {"src/payouts/rounding.py": PAYOUT_ROUNDING}),
    ("storefront", "@sam", "2025-08-21T11:00:00Z", "session cookie for checkout", {"src/lib/cookies.ts": SESSION_COOKIES}),
]


def load(name: str) -> dict:
    return tomllib.loads((HERE / name).read_text())
