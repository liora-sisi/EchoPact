"""Cross-run reservations for one explicitly approved, bounded experiment."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path

from .caretaker_summary import SummaryError
from .venice_summary_trial import MAX_SUPPORTED_BUDGET_USD


class BudgetLedger:
    """Existing ledger only; exclusive lock; crashes keep reservations and fail closed.

    Seed the ledger separately from reviewed earlier receipts. No CLI can reset it.
    An interrupted process may leave the lock behind; reconciliation is manual.
    """

    def __init__(self, path: Path, *, maximum_limit=MAX_SUPPORTED_BUDGET_USD):
        if (not isinstance(maximum_limit, Decimal) or not maximum_limit.is_finite()
                or not 0 < maximum_limit <= Decimal("0.50")):
            raise SummaryError("invalid_trial_budget_ceiling")
        self.maximum_limit = maximum_limit
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")
        self.lock_fd = None
        self.data = None

    def __enter__(self):
        try:
            self.lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise SummaryError("budget_ledger_locked_reconcile_before_retry") from None
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(self.data, dict) or self.data.get("schema") != "echo-summary-budget-v1"
                    or not isinstance(self.data.get("reservations"), list)):
                raise ValueError
            self.total  # Validate the inherited reservations before any key read or API request.
        except Exception:
            self.__exit__(None, None, None)
            raise SummaryError("invalid_existing_budget_ledger") from None
        return self

    @property
    def limit(self) -> Decimal:
        try:
            value = self.data["limit_usd"]
            if not isinstance(value, str):
                raise ValueError
            limit = Decimal(value)
            if not limit.is_finite() or not 0 < limit <= self.maximum_limit:
                raise ValueError
            return limit
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise SummaryError("invalid_existing_budget_ledger") from None

    @property
    def total(self) -> Decimal:
        try:
            values = [Decimal(self.data["prior_reserved_usd"])]
            values += [Decimal(row["reserved_usd"]) for row in self.data["reservations"]]
            if any(not x.is_finite() or x < 0 for x in values):
                raise ValueError
            total = sum(values, Decimal("0"))
            if total > self.limit:
                raise ValueError
            return total
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise SummaryError("invalid_existing_budget_ledger") from None

    def reserve(self, *, amount: Decimal, run_id: str, model: str, case_id: str):
        if (self.lock_fd is None or not amount.is_finite() or amount <= 0
                or self.total + amount > self.limit):
            raise SummaryError("trial_budget_exhausted")
        self.data["reservations"].append({"run_id": run_id, "model": model, "case_id": case_id,
            "reserved_usd": str(amount), "at": datetime.now(timezone.utc).isoformat()})
        temporary = self.path.with_name(self.path.name + ".pending")
        # Never reuse a pending write after an interrupted attempt.
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(self.data, ensure_ascii=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def __exit__(self, *args):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
            self.lock_path.unlink()
