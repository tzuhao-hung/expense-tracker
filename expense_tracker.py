"""
Expense tracking backend with personal and shared (Splitwise-like) logic.

Core features implemented here:
- Personal income/expense recording with monthly summaries.
- Shared expenses with percentage or fixed splits.
- Balance calculation showing who owes whom.
- Monthly analysis combining personal and shared spending.
"""

from __future__ import annotations

import calendar
import os
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple


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
    def __init__(self, db_path: str = "expenses.db") -> None:
        # check_same_thread=False allows use across Flask threads; for production,
        # prefer scoped connections per request.
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        """Create tables if they do not already exist."""
        self.conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS personal_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                type TEXT NOT NULL CHECK (type IN ('income', 'expense')),
                amount REAL NOT NULL CHECK (amount > 0),
                category TEXT NOT NULL,
                note TEXT DEFAULT '',
                date TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shared_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                total_amount REAL NOT NULL CHECK (total_amount > 0),
                date TEXT NOT NULL,
                paid_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                category TEXT NOT NULL DEFAULT 'others',
                note TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS shared_expense_splits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shared_expense_id INTEGER NOT NULL REFERENCES shared_expenses(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                split_type TEXT NOT NULL CHECK (split_type IN ('percentage', 'fixed')),
                split_value REAL NOT NULL CHECK (split_value >= 0)
            );

            CREATE INDEX IF NOT EXISTS idx_personal_transactions_user_date
                ON personal_transactions(user_id, date);

            CREATE INDEX IF NOT EXISTS idx_shared_expenses_date
                ON shared_expenses(date);
            """
        )
        self.conn.commit()

    # --- User management -------------------------------------------------
    def add_user(self, name: str) -> int:
        """Insert a new user; returns the user id."""
        cur = self.conn.execute(
            "INSERT INTO users(name) VALUES (?)",
            (name.strip(),),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_users(self) -> List[sqlite3.Row]:
        cur = self.conn.execute("SELECT id, name FROM users ORDER BY name")
        return list(cur.fetchall())

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
        cur = self.conn.execute(
            """
            INSERT INTO personal_transactions(user_id, type, amount, category, note, date)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, tx_type, amount, category, note, tx_date),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_personal_transactions(
        self,
        user_id: int,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM personal_transactions WHERE user_id = ?"
        params: List[object] = [user_id]
        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)
        sql += " ORDER BY date DESC, id DESC"
        cur = self.conn.execute(sql, params)
        return list(cur.fetchall())

    def personal_monthly_summary(self, user_id: int, year: int, month: int) -> Dict[str, float]:
        start, end = month_bounds(year, month)
        cur = self.conn.execute(
            """
            SELECT
                SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END) AS income,
                SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END) AS expenses
            FROM personal_transactions
            WHERE user_id = ? AND date BETWEEN ? AND ?
            """,
            (user_id, start, end),
        )
        row = cur.fetchone()
        income = row["income"] or 0.0
        expenses = row["expenses"] or 0.0
        return {"income": income, "expenses": expenses, "savings": income - expenses}

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
        """
        Add a shared expense.

        splits: iterable of dicts with keys:
            - user_id: int
            - split_type: 'percentage' or 'fixed'
            - value: numeric percentage or fixed amount
        """
        if total_amount <= 0:
            raise ValueError("total_amount must be positive")

        # Normalize splits and pre-validate.
        normalized_splits = []
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

        shares = self._compute_shares_from_splits(total_amount, normalized_splits)

        cur = self.conn.execute(
            """
            INSERT INTO shared_expenses(title, total_amount, date, paid_by_user_id, category, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (title, total_amount, expense_date, paid_by_user_id, category, note),
        )
        expense_id = int(cur.lastrowid)

        for split in normalized_splits:
            self.conn.execute(
                """
                INSERT INTO shared_expense_splits(shared_expense_id, user_id, split_type, split_value)
                VALUES (?, ?, ?, ?)
                """,
                (expense_id, split["user_id"], split["split_type"], split["value"]),
            )

        self.conn.commit()
        return expense_id

    def _compute_shares_from_splits(
        self, total_amount: float, splits: Iterable[Dict[str, object]]
    ) -> Dict[int, float]:
        """
        Convert stored splits to actual monetary shares.
        Auto-normalizes percentage splits to fill the remaining amount (helps if a participant was removed).
        """

        def _split_value(split: object) -> float:
            # Support both incoming dicts (from forms) and sqlite3.Row objects (from DB).
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
        # Apply fixed shares first.
        for split in splits:
            if split["split_type"] == "fixed":
                uid = int(split["user_id"])
                val = _split_value(split)
                shares[uid] = shares.get(uid, 0.0) + val

        remaining = total_amount - fixed_total
        if remaining < -1e-6:
            raise ValueError("Allocated exceeds total amount")

        # Allocate remaining by percentage (scaled) or equally if no percentages.
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
        cur = self.conn.execute("SELECT * FROM shared_expenses WHERE id = ?", (expense_id,))
        expense = cur.fetchone()
        if not expense:
            raise ValueError("Shared expense not found")
        split_rows = self.conn.execute(
            "SELECT user_id, split_type, split_value FROM shared_expense_splits WHERE shared_expense_id = ?",
            (expense_id,),
        ).fetchall()
        shares = self._compute_shares_from_splits(expense["total_amount"], split_rows)
        return {
            "expense": dict(expense),
            "splits": [dict(row) for row in split_rows],
            "shares": shares,
        }

    def calculate_shared_balances(self) -> Dict[str, object]:
        """
        Return overall balances for all shared expenses.
        net_by_user: positive means the user should receive money.
        settlements: simplified payer -> receiver transfers to clear balances.
        """
        expenses = self.conn.execute("SELECT * FROM shared_expenses ORDER BY date").fetchall()
        if not expenses:
            return {"net_by_user": {}, "settlements": []}

        net: Dict[int, float] = {}
        for expense in expenses:
            split_rows = self.conn.execute(
                """
                SELECT user_id, split_type, split_value
                FROM shared_expense_splits
                WHERE shared_expense_id = ?
                """,
                (expense["id"],),
            ).fetchall()
            shares = self._compute_shares_from_splits(expense["total_amount"], split_rows)
            payer = int(expense["paid_by_user_id"])

            # Paid amount increases payer's balance; each share decreases participant balance.
            net[payer] = net.get(payer, 0.0) + float(expense["total_amount"])
            for uid, share in shares.items():
                net[uid] = net.get(uid, 0.0) - share

        settlements = self._settle(net)
        return {"net_by_user": net, "settlements": [s.__dict__ for s in settlements]}

    def _settle(self, net_by_user: Dict[int, float]) -> List[Settlement]:
        """Generate transfers to resolve balances (greedy creditor/debtor matching)."""
        creditors: List[Tuple[int, float]] = [
            (uid, bal) for uid, bal in net_by_user.items() if bal > 0.009
        ]
        debtors: List[Tuple[int, float]] = [
            (uid, -bal) for uid, bal in net_by_user.items() if bal < -0.009
        ]

        # Largest-first makes results predictable and keeps transfer count small.
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
        """
        Combined monthly view for all users:
        - per user income/expenses/savings (personal + shared shares)
        - combined totals
        - spending by category
        """
        start, end = month_bounds(year, month)
        users = self.list_users()
        per_user = {row["id"]: {"name": row["name"]} for row in users}

        # Personal income/expenses
        for user in users:
            summary = self.personal_monthly_summary(user["id"], year, month)
            per_user[user["id"]].update(
                {
                    "personal_expenses": summary["expenses"],
                    "personal_income": summary["income"],
                    "shared_share": 0.0,
                }
            )

        # Shared expense shares during the month
        shared = self.conn.execute(
            "SELECT * FROM shared_expenses WHERE date BETWEEN ? AND ?",
            (start, end),
        ).fetchall()

        for expense in shared:
            split_rows = self.conn.execute(
                """
                SELECT user_id, split_type, split_value
                FROM shared_expense_splits
                WHERE shared_expense_id = ?
                """,
                (expense["id"],),
            ).fetchall()
            shares = self._compute_shares_from_splits(expense["total_amount"], split_rows)
            for uid, share in shares.items():
                per_user[uid]["shared_share"] = per_user[uid].get("shared_share", 0.0) + share

        # Finalize totals and savings per user.
        for uid, data in per_user.items():
            total_expenses = data["personal_expenses"] + data["shared_share"]
            data["total_expenses"] = total_expenses
            data["savings"] = data["personal_income"] - total_expenses

        # Combined totals
        combined_income = sum(d["personal_income"] for d in per_user.values())
        combined_expenses = sum(d["total_expenses"] for d in per_user.values())
        combined = {
            "income": combined_income,
            "expenses": combined_expenses,
            "savings": combined_income - combined_expenses,
        }

        # Category breakdown from personal expenses + shared expense categories.
        category_breakdown: Dict[str, float] = {}
        personal_rows = self.conn.execute(
            """
            SELECT category, SUM(amount) AS total
            FROM personal_transactions
            WHERE type = 'expense' AND date BETWEEN ? AND ?
            GROUP BY category
            """,
            (start, end),
        ).fetchall()
        for row in personal_rows:
            category_breakdown[row["category"]] = category_breakdown.get(row["category"], 0.0) + (
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
    # Minimal example usage.
    tracker = ExpenseTracker()
    if not tracker.list_users():
        alice = tracker.add_user("Alice")
        bob = tracker.add_user("Bob")
        print(f"Created users Alice={alice}, Bob={bob}")
    else:
        users = tracker.list_users()
        alice, bob = users[0]["id"], users[1]["id"] if len(users) > 1 else users[0]["id"]

    # Add a personal transaction
    tracker.add_personal_transaction(alice, "income", 2000, "2023-12-01", "salary", "Monthly paycheck")

    # Add a shared dinner split 60/40
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
