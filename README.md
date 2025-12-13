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

## 免費部署推薦：Render Free + Neon Postgres
- 你不需要付費磁碟，資料放在免費的雲端 Postgres（Neon）。
- 環境變數：
  - `SECRET_KEY`: 強隨機字串
  - `DATABASE_URL`: Neon 提供的連線字串（格式 `postgresql://user:pass@host/db`)

步驟：
1. 在 Neon 建立免費 Postgres 專案，複製 `postgresql://...` 連線字串。
2. 在 Render 新建 Blueprint：連結 GitHub repo，分支 main；render.yaml 會自動建立 Web 服務，Start Command `gunicorn app:app --bind 0.0.0.0:$PORT`，方案選 free。
3. 在 Render 服務設定環境變數：`SECRET_KEY`、`DATABASE_URL`（貼 Neon 的連線字串）。
4. Deploy，完成後取得網址，手機直接使用。

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
