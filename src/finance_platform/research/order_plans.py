"""Immutable strategy decision plans, persisted before shared OMS effects."""
from finance_platform.core.database import immutable

MIGRATIONS=("""
CREATE TABLE research_order_plans (
 account_id TEXT NOT NULL REFERENCES trading_accounts(account_id),output_id TEXT NOT NULL,
 output_hash TEXT NOT NULL,sample_hash TEXT NOT NULL,intent_json TEXT,risk_version TEXT NOT NULL,
 risk_hash TEXT NOT NULL,decision_at TEXT NOT NULL,planned_at TEXT NOT NULL,
 PRIMARY KEY(account_id,output_id)
);
""",)


def install(database):
    database.apply_module_migrations("research_order_plans",MIGRATIONS)
    with database.transaction() as con:
        immutable(con,"research_order_plans",("account_id","output_id"))
