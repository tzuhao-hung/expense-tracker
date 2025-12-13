# Expense Tracker (Splitwise-style)

Python + SQLite backend for personal and shared expense tracking. A lightweight Flask UI is included in `app.py`.

## Database schema
- `users(id, name)`
- `personal_transactions(id, user_id, type, amount, category, note, date)`
  - `type` is `income` or `expense`
  - `date` stored as `YYYY-MM-DD`
- `shared_expenses(id, title, total_amount, date, paid_by_user_id, category, note)`
- `shared_expense_splits(id, shared_expense_id, user_id, split_type, split_value)`
  - `split_type` is `percentage` or `fixed`
  - `split_value` is the percent or fixed amount

Foreign keys cascade deletes; indexes cover date/user queries.

## Core logic
Implemented in `expense_tracker.py`:
- Personal CRUD: add/list personal transactions, monthly summary (income, expenses, savings).
- Shared expenses: add expenses with percentage/fixed splits; payer must be included; validation enforces the split covers the total.
- Balances: aggregate all shared expenses to show net per user and a greedy settlement list (payer -> receiver).
- Monthly analysis: per-user totals (personal + shared share), combined household totals, and category breakdown.

## Quick start
```bash
pip install flask
python app.py
# open http://127.0.0.1:5000
```

## 部署到 Render（例）
1. 在專案根目錄新增 `requirements.txt`（已包含 Flask、gunicorn）與 `Procfile`（內容 `web: gunicorn app:app --bind 0.0.0.0:$PORT`）。
2. 將專案推到 GitHub。
3. Render 建立 New Web Service：連 GitHub repo，Environment 選 Python，Start Command 填 `gunicorn app:app --bind 0.0.0.0:$PORT`。
4. 新增環境變數：`SECRET_KEY`（強隨機字串）、`DB_PATH`（例如 `/var/data/expenses.db`）。
5. 加掛 Disk（例：Mount Path `/var/data`）讓 SQLite 檔持久化；若 Mount 到其他路徑，將 `DB_PATH` 指向該檔。
6. Deploy，拿到網址後手機直接使用。

## Usage guide (web)
- Dashboard: shows monthly totals, per-user spending, category breakdown, and suggested settlements. Select month/year in the header.
- Add user: form on the dashboard (names must be unique).
- Add personal transaction: `Personal` nav item; choose user, type, category, amount, and date.
- Add shared expense: `Shared` nav item; enter total, payer, and add participant rows with percentage or fixed splits (payer must be listed).
- Balances: `Balances` nav item; shows who owes whom across all shared expenses.
- Delete user: dashboard list; deletes their splits and any shared expenses they paid (history will be removed).

## Direct CLI sample
```bash
python expense_tracker.py
```
The `__main__` block seeds two users if none exist, adds example records, prints balances and a monthly analysis. Use the `ExpenseTracker` class from other scripts or a UI layer for production use.
