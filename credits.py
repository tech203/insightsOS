import json
import os
from datetime import datetime


class CreditLedger:
    def __init__(self, db_path: str = "data/credits.json"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        if not os.path.exists(self.db_path):
            self._write({"balances": {}, "transactions": []})

    def _read(self):
        with open(self.db_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data):
        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def set_balance(self, user_id: str, workspace_id: str, credits: int):
        data = self._read()
        key = f"{user_id}:{workspace_id}"
        data["balances"][key] = credits
        self._write(data)

    def get_balance(self, user_id: str, workspace_id: str) -> int:
        data = self._read()
        key = f"{user_id}:{workspace_id}"
        return data["balances"].get(key, 0)

    def has_enough(self, user_id: str, workspace_id: str, needed: int) -> bool:
        return self.get_balance(user_id, workspace_id) >= needed

    def deduct(self, user_id: str, workspace_id: str, amount: int, action: str):
        data = self._read()
        key = f"{user_id}:{workspace_id}"
        current = data["balances"].get(key, 0)

        if current < amount:
            raise ValueError("Insufficient credits")

        data["balances"][key] = current - amount
        data["transactions"].append({
            "user_id": user_id,
            "workspace_id": workspace_id,
            "action": action,
            "amount": -amount,
            "timestamp": datetime.utcnow().isoformat(),
        })
        self._write(data)