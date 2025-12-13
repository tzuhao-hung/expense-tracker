"""
Expense tracking backend with personal and shared (Splitwise-like) logic.

Now uses SQLAlchemy so it can run on SQLite locally or Postgres in the cloud.
"""

from __future__ import annotations

import calendar
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import (
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    case,
    create_engine,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine, Result
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


DEFAULT_CATEGORIES = [
    "grocery",
    "clothing",
    "entertainment",
    "dining",
    "rent",
    "transportation",
    "others",
]


@dataclass
class Settlement:
    payer_id: int
    receiver_id: int
    amount: float


class ExpenseTracker:
    def __init__(self, db_url: str = "sqlite:///expenses.db") -> None:
        self.engine: Engine = create_engine(db_url, future=True, echo=False)
        self.metadata = MetaData()

        # Table definitions
        self.users = self._define_users()
        self.personal_transactions = self._define_personal_transactions()
        self.shared_expenses = self._define_shared_expenses()
        self.shared_expense_splits = self._define_shared_expense_splits()

        # Indexes
        Index(
            "idx_personal_transactions_user_date",
            self.personal_transactions.c.user_id,
            self.personal_transactions.c.date,
        )
        Index("idx_shared_expenses_date", self.shared_expenses.c.date)

        self.metadata.create_all(self.engine)

    def _define_users(self):
        return self._table(
            "users",
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("name", String, nullable=False, unique=True),
        )

    def _define_personal_transactions(self):
        return self._table(
            "personal_transactions",
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            Column("type", String, nullable=False),
            Column("amount", Float, nullable=False),
            Column("category", String, nullable=False),
            Column("note", Text, nullable=False, default=""),
            Column("date", String, nullable=False),
            CheckConstraint("type IN ('income','expense')", name="chk_pt_type"),
            CheckConstraint("amount > 0", name="chk_pt_amount"),
        )

    def _define_shared_expenses(self):
        return self._table(
            "shared_expenses",
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("title", String, nullable=False),
            Column("total_amount", Float, nullable=False),
            Column("date", String, nullable=False),
            Column("paid_by_user_id", Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
            Column("category", String, nullable=False, default="others"),
            Column("note", Text, nullable=False, default=""),
            CheckConstraint("total_amount > 0", name="chk_se_total"),
        )

    def _define_shared_expense_splits(self):
        return self._table(
            "shared_expense_splits",
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column(
                "shared_expense_id",
                Integer,
                ForeignKey("shared_expenses.id", ondelete="CASCADE"),
                nullable=False,
            ),
            Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            Column("split_type", String, nullable=False),
            Column("split_value", Float, nullable=False),
            CheckConstraint("split_type IN ('percentage','fixed')", name="chk_split_type"),
            CheckConstraint("split_value >= 0", name="chk_split_value"),
        )

    def _table(self, name: str, *cols: Any):
        return self.metadata.tables.get(name) or self._create_table(name, *cols)

    def _create_table(self, name: str, *cols: Any):
        return self.metadata.tables.setdefault(name, self.metadata.tables.get(name) or self._make_table(name, *cols))

    def _make_table(self, name: str, *cols: Any):
        from sqlalchemy import Table

        return Table(name, self.metadata, *cols)

    @contextmanager
    def _session(self) -> Iterable[Session]:
        with Session(self.engine) as session:
            yield session

    # --- User management -------------------------------------------------
    def add_user(self, name: str) -> int:
        with self._session() as session:
            res = session.execute(insert(self.users).values(name=name.strip()))
            session.commit()
            return int(res.inserted_primary_key[0])

    def find_user_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self._session() as session:
            stmt = select(self.users).where(func.lower(self.users.c.name) == func.lower(name.strip()))
            row = session.execute(stmt).mappings().first()
            return dict(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        with self._session() as session:
            res = session.execute(select(self.users).order_by(self.users.c.name)).mappings().all()
            return [dict(row) for row in res]

    def delete_user(self, user_id: int) -> None:
        with self._session() as session:
            session.execute(
                delete(self.shared_expense_splits).where(self.shared_expense_splits.c.user_id == user_id)
            )
            session.execute(delete(self.shared_expenses).where(self.shared_expenses.c.paid_by_user_id == user_id))
            session.execute(delete(self.users).where(self.users.c.id == user_id))
            session.commit()

    # --- Personal transactions ------------------------------------------
    def add_personal_transaction(
        self,
        user_id: int,
        tx_type: str,
        amount: float,
        tx_date: str,
        category: str,
        note: str = "",
    ) -> int:
        if tx_type not in {"income", "expense"}:
            raise ValueError("tx_type must be 'income' or 'expense'")
        if amount <= 0:
            raise ValueError("amount must be positive")
        with self._session() as session:
            res = session.execute(
                insert(self.personal_transactions).values(
                    user_id=user_id,
                    type=tx_type,
                    amount=amount,
                    category=category,
                    note=note,
                    date=tx_date,
                )
            )
            session.commit()
            return int(res.inserted_primary_key[0])

    def get_personal_transactions(
        self,
        user_id: int,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        stmt = select(self.personal_transactions).where(self.personal_transactions.c.user_id == user_id)
        if start_date:
            stmt = stmt.where(self.personal_transactions.c.date >= start_date)
        if end_date:
            stmt = stmt.where(self.personal_transactions.c.date <= end_date)
        stmt = stmt.order_by(self.personal_transactions.c.date.desc(), self.personal_transactions.c.id.desc())
        with self._session() as session:
            res = session.execute(stmt).mappings().all()
            return [dict(row) for row in res]

    def get_personal_transaction(self, tx_id: int) -> Optional[Dict[str, Any]]:
        with self._session() as session:
            row = session.execute(
                select(self.personal_transactions).where(self.personal_transactions.c.id == tx_id)
            ).mappings().first()
            return dict(row) if row else None

    def update_personal_transaction(
        self,
        tx_id: int,
        user_id: int,
        tx_type: str,
        amount: float,
        category: str,
        note: str,
        tx_date: str,
    ) -> None:
        with self._session() as session:
            session.execute(
                update(self.personal_transactions)
                .where(self.personal_transactions.c.id == tx_id)
                .values(
                    user_id=user_id,
                    type=tx_type,
                    amount=amount,
                    category=category,
                    note=note,
                    date=tx_date,
                )
            )
            session.commit()

    def delete_personal_transaction(self, tx_id: int) -> None:
        with self._session() as session:
            session.execute(delete(self.personal_transactions).where(self.personal_transactions.c.id == tx_id))
            session.commit()

    def personal_monthly_summary(self, user_id: int, year: int, month: int) -> Dict[str, float]:
        start, end = month_bounds(year, month)
        income_case = case((self.personal_transactions.c.type == "income", self.personal_transactions.c.amount), else_=0)
        expense_case = case(
            (self.personal_transactions.c.type == "expense", self.personal_transactions.c.amount), else_=0
        )
        stmt = (
            select(
                func.coalesce(func.sum(income_case), 0).label("income"),
                func.coalesce(func.sum(expense_case), 0).label("expenses"),
            )
            .where(self.personal_transactions.c.user_id == user_id)
            .where(self.personal_transactions.c.date.between(start, end))
        )
        with self._session() as session:
            row = session.execute(stmt).mappings().first()
            income = float(row["income"])
            expenses = float(row["expenses"])
            return {"income": income, "expenses": expenses, "savings": income - expenses}

    def recent_personal_transactions(self, limit: int = 20) -> List[Dict[str, Any]]:
        stmt = (
            select(
                self.personal_transactions.c.id,
                self.users.c.name.label("user_name"),
                self.personal_transactions.c.type,
                self.personal_transactions.c.amount,
                self.personal_transactions.c.category,
                self.personal_transactions.c.date,
                self.personal_transactions.c.note,
            )
            .join(self.users, self.users.c.id == self.personal_transactions.c.user_id)
            .order_by(self.personal_transactions.c.date.desc(), self.personal_transactions.c.id.desc())
            .limit(limit)
        )
        with self._session() as session:
            res = session.execute(stmt).mappings().all()
            return [dict(row) for row in res]

    # --- Shared expenses -------------------------------------------------
    def add_shared_expense(
        self,
        title: str,
        total_amount: float,
        expense_date: str,
        paid_by_user_id: int,
        splits: Iterable[Dict[str, object]],
        category: str = "others",
        note: str = "",
    ) -> int:
        if total_amount <= 0:
            raise ValueError("total_amount must be positive")

        normalized_splits: List[Dict[str, Any]] = []
        payer_seen = False
        for split in splits:
            user_id = int(split["user_id"])
            split_type = str(split["split_type"])
            value = float(split["value"])
            if split_type not in {"percentage", "fixed"}:
                raise ValueError("split_type must be 'percentage' or 'fixed'")
            if value < 0:
                raise ValueError("split_value cannot be negative")
            if user_id == paid_by_user_id:
                payer_seen = True
            normalized_splits.append({"user_id": user_id, "split_type": split_type, "value": value})

        if not normalized_splits:
            raise ValueError("At least one participant is required")
        if not payer_seen:
            raise ValueError("Payer must be included in splits")

        self._compute_shares_from_splits(total_amount, normalized_splits)

        with self._session() as session:
            res = session.execute(
                insert(self.shared_expenses).values(
                    title=title,
                    total_amount=total_amount,
                    date=expense_date,
                    paid_by_user_id=paid_by_user_id,
                    category=category,
                    note=note,
                )
            )
            expense_id = int(res.inserted_primary_key[0])

            for split in normalized_splits:
                session.execute(
                    insert(self.shared_expense_splits).values(
                        shared_expense_id=expense_id,
                        user_id=split["user_id"],
                        split_type=split["split_type"],
                        split_value=split["value"],
                    )
                )
            session.commit()
            return expense_id

    def _compute_shares_from_splits(
        self, total_amount: float, splits: Iterable[Dict[str, object]]
    ) -> Dict[int, float]:
        def _split_value(split: object) -> float:
            try:
                if isinstance(split, dict):
                    if "value" in split:
                        return float(split["value"])
                    if "split_value" in split:
                        return float(split["split_value"])
                return float(split["value"])  # type: ignore[index]
            except Exception:
                return float(split["split_value"])  # type: ignore[index]

        splits = list(splits)
        if not splits:
            raise ValueError("At least one participant is required")

        fixed_total = 0.0
        percent_total = 0.0
        for split in splits:
            value = _split_value(split)
            if split["split_type"] == "fixed":
                fixed_total += value
            else:
                percent_total += value

        if fixed_total - total_amount > 1e-6:
            raise ValueError("Fixed splits exceed the total amount")

        shares: Dict[int, float] = {}
        for split in splits:
            if split["split_type"] == "fixed":
                uid = int(split["user_id"])
                val = _split_value(split)
                shares[uid] = shares.get(uid, 0.0) + val

        remaining = total_amount - fixed_total
        if remaining < -1e-6:
            raise ValueError("Allocated exceeds total amount")

        if remaining > 1e-6:
            if percent_total > 0:
                for split in splits:
                    if split["split_type"] == "percentage":
                        uid = int(split["user_id"])
                        val = _split_value(split)
                        share = remaining * (val / percent_total)
                        shares[uid] = shares.get(uid, 0.0) + share
            else:
                even_share = remaining / len(splits)
                for split in splits:
                    uid = int(split["user_id"])
                    shares[uid] = shares.get(uid, 0.0) + even_share

        return shares

    def shared_expense_detail(self, expense_id: int) -> Dict[str, object]:
        with self._session() as session:
            expense = (
                session.execute(select(self.shared_expenses).where(self.shared_expenses.c.id == expense_id))
                .mappings()
                .first()
            )
            if not expense:
                raise ValueError("Shared expense not found")
            split_rows = (
                session.execute(
                    select(
                        self.shared_expense_splits.c.user_id,
                        self.shared_expense_splits.c.split_type,
                        self.shared_expense_splits.c.split_value,
                    ).where(self.shared_expense_splits.c.shared_expense_id == expense_id)
                )
                .mappings()
                .all()
            )
        shares = self._compute_shares_from_splits(expense["total_amount"], split_rows)
        return {
            "expense": dict(expense),
            "splits": [dict(row) for row in split_rows],
            "shares": shares,
        }

    def get_shared_expense(self, expense_id: int) -> Optional[Dict[str, Any]]:
        with self._session() as session:
            row = session.execute(
                select(self.shared_expenses).where(self.shared_expenses.c.id == expense_id)
            ).mappings().first()
            return dict(row) if row else None

    def get_shared_splits(self, expense_id: int) -> List[Dict[str, Any]]:
        with self._session() as session:
            res = session.execute(
                select(
                    self.shared_expense_splits.c.user_id,
                    self.shared_expense_splits.c.split_type,
                    self.shared_expense_splits.c.split_value,
                ).where(self.shared_expense_splits.c.shared_expense_id == expense_id)
            ).mappings()
            return [dict(row) for row in res.all()]

    def update_shared_expense(
        self,
        expense_id: int,
        title: str,
        total_amount: float,
        expense_date: str,
        paid_by_user_id: int,
        category: str,
        note: str,
        splits: Iterable[Dict[str, object]],
    ) -> None:
        normalized_splits: List[Dict[str, Any]] = []
        payer_seen = False
        for split in splits:
            uid = int(split["user_id"])
            stype = str(split["split_type"])
            val = float(split["value"])
            if stype not in {"percentage", "fixed"}:
                raise ValueError("split_type must be 'percentage' or 'fixed'")
            if val < 0:
                raise ValueError("split_value cannot be negative")
            if uid == paid_by_user_id:
                payer_seen = True
            normalized_splits.append({"user_id": uid, "split_type": stype, "value": val})
        if not normalized_splits:
            raise ValueError("At least one participant is required")
        if not payer_seen:
            normalized_splits.append({"user_id": paid_by_user_id, "split_type": "percentage", "value": 0})

        self._compute_shares_from_splits(total_amount, normalized_splits)

        with self._session() as session:
            session.execute(
                update(self.shared_expenses)
                .where(self.shared_expenses.c.id == expense_id)
                .values(
                    title=title,
                    total_amount=total_amount,
                    date=expense_date,
                    paid_by_user_id=paid_by_user_id,
                    category=category,
                    note=note,
                )
            )
            session.execute(
                delete(self.shared_expense_splits).where(
                    self.shared_expense_splits.c.shared_expense_id == expense_id
                )
            )
            for split in normalized_splits:
                session.execute(
                    insert(self.shared_expense_splits).values(
                        shared_expense_id=expense_id,
                        user_id=split["user_id"],
                        split_type=split["split_type"],
                        split_value=split["value"],
                    )
                )
            session.commit()

    def delete_shared_expense(self, expense_id: int) -> None:
        with self._session() as session:
            session.execute(
                delete(self.shared_expense_splits).where(self.shared_expense_splits.c.shared_expense_id == expense_id)
            )
            session.execute(delete(self.shared_expenses).where(self.shared_expenses.c.id == expense_id))
            session.commit()

    def recent_shared_expenses(self, limit: int = 20) -> List[Dict[str, Any]]:
        stmt = (
            select(
                self.shared_expenses.c.id,
                self.shared_expenses.c.title,
                self.shared_expenses.c.total_amount,
                self.shared_expenses.c.date,
                self.shared_expenses.c.category,
                self.shared_expenses.c.note,
                self.users.c.name.label("paid_by"),
            )
            .join(self.users, self.users.c.id == self.shared_expenses.c.paid_by_user_id)
            .order_by(self.shared_expenses.c.date.desc(), self.shared_expenses.c.id.desc())
            .limit(limit)
        )
        with self._session() as session:
            res = session.execute(stmt).mappings().all()
            return [dict(row) for row in res]

    def calculate_shared_balances(self) -> Dict[str, object]:
        with self._session() as session:
            expenses = (
                session.execute(select(self.shared_expenses).order_by(self.shared_expenses.c.date)).mappings().all()
            )
            if not expenses:
                return {"net_by_user": {}, "settlements": []}

            net: Dict[int, float] = {}
            for expense in expenses:
                split_rows = (
                    session.execute(
                        select(
                            self.shared_expense_splits.c.user_id,
                            self.shared_expense_splits.c.split_type,
                            self.shared_expense_splits.c.split_value,
                        ).where(self.shared_expense_splits.c.shared_expense_id == expense["id"])
                    )
                    .mappings()
                    .all()
                )
                shares = self._compute_shares_from_splits(expense["total_amount"], split_rows)
                payer = int(expense["paid_by_user_id"])
                net[payer] = net.get(payer, 0.0) + float(expense["total_amount"])
                for uid, share in shares.items():
                    net[uid] = net.get(uid, 0.0) - share

        settlements = self._settle(net)
        return {"net_by_user": net, "settlements": [s.__dict__ for s in settlements]}

    def _settle(self, net_by_user: Dict[int, float]) -> List[Settlement]:
        creditors: List[Tuple[int, float]] = [(uid, bal) for uid, bal in net_by_user.items() if bal > 0.009]
        debtors: List[Tuple[int, float]] = [(uid, -bal) for uid, bal in net_by_user.items() if bal < -0.009]

        creditors.sort(key=lambda x: x[1], reverse=True)
        debtors.sort(key=lambda x: x[1], reverse=True)

        settlements: List[Settlement] = []
        ci = di = 0
        while ci < len(creditors) and di < len(debtors):
            cred_id, cred_amt = creditors[ci]
            debt_id, debt_amt = debtors[di]
            pay = min(cred_amt, debt_amt)
            settlements.append(Settlement(payer_id=debt_id, receiver_id=cred_id, amount=round(pay, 2)))

            cred_amt -= pay
            debt_amt -= pay
            creditors[ci] = (cred_id, cred_amt)
            debtors[di] = (debt_id, debt_amt)

            if cred_amt <= 0.009:
                ci += 1
            if debt_amt <= 0.009:
                di += 1
        return settlements

    # --- Monthly analysis -----------------------------------------------
    def monthly_analysis(self, year: int, month: int) -> Dict[str, object]:
        start, end = month_bounds(year, month)
        users = self.list_users()
        per_user: Dict[int, Dict[str, Any]] = {row["id"]: {"name": row["name"]} for row in users}

        for user in users:
            summary = self.personal_monthly_summary(user["id"], year, month)
            per_user[user["id"]].update(
                {
                    "personal_expenses": summary["expenses"],
                    "personal_income": summary["income"],
                    "shared_share": 0.0,
                }
            )

        with self._session() as session:
            shared = (
                session.execute(
                    select(self.shared_expenses).where(self.shared_expenses.c.date.between(start, end))
                ).mappings().all()
            )

            for expense in shared:
                split_rows = (
                    session.execute(
                        select(
                            self.shared_expense_splits.c.user_id,
                            self.shared_expense_splits.c.split_type,
                            self.shared_expense_splits.c.split_value,
                        ).where(self.shared_expense_splits.c.shared_expense_id == expense["id"])
                    )
                    .mappings()
                    .all()
                )
                shares = self._compute_shares_from_splits(expense["total_amount"], split_rows)
                for uid, share in shares.items():
                    per_user[uid]["shared_share"] = per_user[uid].get("shared_share", 0.0) + share

            for uid, data in per_user.items():
                total_expenses = data["personal_expenses"] + data["shared_share"]
                data["total_expenses"] = total_expenses
                data["savings"] = data["personal_income"] - total_expenses

            combined_income = sum(d["personal_income"] for d in per_user.values())
            combined_expenses = sum(d["total_expenses"] for d in per_user.values())
            combined = {
                "income": combined_income,
                "expenses": combined_expenses,
                "savings": combined_income - combined_expenses,
            }

            category_breakdown: Dict[str, float] = {}
            personal_rows = session.execute(
                select(self.personal_transactions.c.category, func.sum(self.personal_transactions.c.amount).label("total"))
                .where(self.personal_transactions.c.type == "expense")
                .where(self.personal_transactions.c.date.between(start, end))
                .group_by(self.personal_transactions.c.category)
            ).mappings().all()
            for row in personal_rows:
                category_breakdown[row["category"]] = category_breakdown.get(row["category"], 0.0) + float(
                    row["total"] or 0.0
                )
            for expense in shared:
                category_breakdown[expense["category"]] = category_breakdown.get(expense["category"], 0.0) + float(
                    expense["total_amount"]
                )

        return {
            "per_user": per_user,
            "combined": combined,
            "category_breakdown": category_breakdown,
        }


def month_bounds(year: int, month: int) -> Tuple[str, str]:
    """Return first and last day strings (YYYY-MM-DD) for a given month."""
    last_day = calendar.monthrange(year, month)[1]
    start = date(year, month, 1).isoformat()
    end = date(year, month, last_day).isoformat()
    return start, end


if __name__ == "__main__":
    tracker = ExpenseTracker()
    if not tracker.list_users():
        alice = tracker.add_user("Alice")
        bob = tracker.add_user("Bob")
        print(f"Created users Alice={alice}, Bob={bob}")
    else:
        users = tracker.list_users()
        alice, bob = users[0]["id"], users[1]["id"] if len(users) > 1 else users[0]["id"]

    tracker.add_personal_transaction(alice, "income", 2000, "2023-12-01", "salary", "Monthly paycheck")
    tracker.add_shared_expense(
        title="Dinner",
        total_amount=80.0,
        expense_date="2023-12-02",
        paid_by_user_id=alice,
        splits=[
            {"user_id": alice, "split_type": "percentage", "value": 60},
            {"user_id": bob, "split_type": "percentage", "value": 40},
        ],
        category="dining",
    )

    print("Balances:", tracker.calculate_shared_balances())
    print("Monthly analysis:", tracker.monthly_analysis(2023, 12))
