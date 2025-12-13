import os
from datetime import date
from typing import Dict

from flask import Flask, flash, redirect, render_template, request, url_for

from expense_tracker import DEFAULT_CATEGORIES, ExpenseTracker


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev")  # Set SECRET_KEY in production.
db_url = os.environ.get("DATABASE_URL")
if not db_url:
    db_path = os.environ.get("DB_PATH", "expenses.db")
    db_url = f"sqlite:///{db_path}"
tracker = ExpenseTracker(db_url)


def _current_year_month() -> Dict[str, int]:
    today = date.today()
    return {"year": today.year, "month": today.month}


def _get_year_month() -> Dict[str, int]:
    """Read year/month query params, defaulting to current month."""
    params = _current_year_month()
    try:
        if request.args.get("year") and request.args.get("month"):
            params["year"] = int(request.args["year"])
            params["month"] = int(request.args["month"])
    except ValueError:
        flash("Invalid year or month, using current month.")
    return params


def _user_map() -> Dict[int, str]:
    return {row["id"]: row["name"] for row in tracker.list_users()}


def _get_or_create_user(name: str) -> int:
    clean = name.strip()
    if not clean:
        raise ValueError("Name is required")
    existing = tracker.find_user_by_name(clean)
    if existing:
        return int(existing["id"])
    return tracker.add_user(clean)


@app.route("/")
def dashboard():
    ym = _get_year_month()
    analysis = tracker.monthly_analysis(ym["year"], ym["month"])
    balances = tracker.calculate_shared_balances()
    balances["usernames"] = _user_map()
    users = tracker.list_users()
    users_json = [dict(u) for u in users]
    personal_entries = tracker.recent_personal_transactions()
    shared_entries = tracker.recent_shared_expenses()
    chart_data = {
        "categories": analysis["category_breakdown"],
        "per_user_spend": {uid: data["total_expenses"] for uid, data in analysis["per_user"].items()},
        "user_names": {u["id"]: u["name"] for u in users},
    }
    return render_template(
        "dashboard.html",
        users=users,
        analysis=analysis,
        balances=balances,
        ym=ym,
        chart_data=chart_data,
        categories=DEFAULT_CATEGORIES,
        today=date.today().isoformat(),
        users_json=users_json,
        personal_entries=personal_entries,
        shared_entries=shared_entries,
    )


@app.route("/users", methods=["POST"])
def create_user():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name is required.")
        return redirect(url_for("dashboard"))
    try:
        tracker.add_user(name)
        flash(f"User '{name}' added.")
    except Exception as exc:  # sqlite3.IntegrityError for duplicates
        flash(f"Could not add user: {exc}")
    return redirect(url_for("dashboard"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id: int):
    try:
        tracker.delete_user(user_id)
        flash("User and related shared expenses deleted.")
    except Exception as exc:
        flash(f"Could not delete user: {exc}")
    return redirect(url_for("dashboard"))


@app.route("/personal/new", methods=["GET", "POST"])
def add_personal():
    users = tracker.list_users()
    if request.method == "POST":
        try:
            name = request.form.get("user_name", "").strip()
            if name:
                user_id = _get_or_create_user(name)
            else:
                user_id = int(request.form["user_id"])
            tracker.add_personal_transaction(
                user_id=user_id,
                tx_type=request.form["type"],
                amount=float(request.form["amount"]),
                tx_date=request.form["date"],
                category=request.form["category"],
                note=request.form.get("note", ""),
            )
            flash("Personal transaction saved.")
            return redirect(url_for("dashboard"))
        except Exception as exc:
            flash(f"Error saving transaction: {exc}")
    return render_template(
        "personal_form.html",
        users=users,
        categories=DEFAULT_CATEGORIES,
        today=date.today().isoformat(),
    )


@app.route("/personal/<int:tx_id>/edit", methods=["GET", "POST"])
def edit_personal(tx_id: int):
    users = tracker.list_users()
    tx = tracker.get_personal_transaction(tx_id)
    if not tx:
        flash("Personal transaction not found.")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        try:
            user_id = int(request.form["user_id"])
            tracker.update_personal_transaction(
                tx_id=tx_id,
                user_id=user_id,
                tx_type=request.form["type"],
                amount=float(request.form["amount"]),
                category=request.form["category"],
                note=request.form.get("note", ""),
                tx_date=request.form["date"],
            )
            flash("Personal transaction updated.")
            return redirect(url_for("dashboard"))
        except Exception as exc:
            flash(f"Error updating: {exc}")
    return render_template(
        "personal_form.html",
        users=users,
        categories=DEFAULT_CATEGORIES,
        today=tx["date"],
        tx=tx,
    )


@app.route("/personal/<int:tx_id>/delete", methods=["POST"])
def delete_personal(tx_id: int):
    try:
        tracker.delete_personal_transaction(tx_id)
        flash("Personal transaction deleted.")
    except Exception as exc:
        flash(f"Could not delete: {exc}")
    return redirect(url_for("dashboard"))


@app.route("/shared/new", methods=["GET", "POST"])
def add_shared():
    users = tracker.list_users()
    if not users:
        flash("Add at least one user before creating shared expenses.")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        total = request.form.get("total_amount", "0")
        paid_by = request.form.get("paid_by")
        expense_date = request.form.get("date", "")
        category = request.form.get("category", "others")
        note = request.form.get("note", "")

        participant_ids = request.form.getlist("participant_user_id")
        participant_types = request.form.getlist("participant_split_type")
        participant_values = request.form.getlist("participant_value")

        splits = []
        for uid, stype, val in zip(participant_ids, participant_types, participant_values):
            if not uid:
                continue
            splits.append({"user_id": int(uid), "split_type": stype, "value": float(val or 0)})

        if not title:
            flash("Title is required.")
            return redirect(url_for("add_shared"))

        try:
            payer_id = int(paid_by)
            if not any(s["user_id"] == payer_id for s in splits):
                # Auto-include payer so the entry is valid even if not listed below.
                splits.append({"user_id": int(payer_id), "split_type": "percentage", "value": 0})
            tracker.add_shared_expense(
                title=title,
                total_amount=float(total),
                expense_date=expense_date,
                paid_by_user_id=int(payer_id),
                splits=splits,
                category=category,
                note=note,
            )
            flash("Shared expense saved.")
            return redirect(url_for("dashboard"))
        except Exception as exc:
            flash(f"Error saving shared expense: {exc}")

    return render_template(
        "shared_form.html",
        users=users,
        categories=DEFAULT_CATEGORIES,
        today=date.today().isoformat(),
    )


@app.route("/shared/<int:expense_id>/edit", methods=["GET", "POST"])
def edit_shared(expense_id: int):
    users = tracker.list_users()
    expense = tracker.get_shared_expense(expense_id)
    if not expense:
        flash("Shared expense not found.")
        return redirect(url_for("dashboard"))
    splits = tracker.get_shared_splits(expense_id)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        total = request.form.get("total_amount", "0")
        paid_by = request.form.get("paid_by")
        expense_date = request.form.get("date", "")
        category = request.form.get("category", "others")
        note = request.form.get("note", "")
        participant_ids = request.form.getlist("participant_user_id")
        participant_types = request.form.getlist("participant_split_type")
        participant_values = request.form.getlist("participant_value")
        new_splits = []
        for uid, stype, val in zip(participant_ids, participant_types, participant_values):
            if not uid:
                continue
            new_splits.append({"user_id": int(uid), "split_type": stype, "value": float(val or 0)})
        try:
            payer_id = int(paid_by)
            tracker.update_shared_expense(
                expense_id=expense_id,
                title=title,
                total_amount=float(total),
                expense_date=expense_date,
                paid_by_user_id=payer_id,
                category=category,
                note=note,
                splits=new_splits,
            )
            flash("Shared expense updated.")
            return redirect(url_for("dashboard"))
        except Exception as exc:
            flash(f"Error updating shared expense: {exc}")
    return render_template(
        "shared_form.html",
        users=users,
        categories=DEFAULT_CATEGORIES,
        today=expense["date"],
        expense=expense,
        splits=splits,
    )


@app.route("/shared/<int:expense_id>/delete", methods=["POST"])
def delete_shared(expense_id: int):
    try:
        tracker.delete_shared_expense(expense_id)
        flash("Shared expense deleted.")
    except Exception as exc:
        flash(f"Could not delete shared expense: {exc}")
    return redirect(url_for("dashboard"))


@app.route("/balances")
def balances():
    balances = tracker.calculate_shared_balances()
    return render_template("balances.html", balances=balances, user_map=_user_map())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
