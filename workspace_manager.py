import json
import os
import uuid
from datetime import datetime


class WorkspaceManager:
    def __init__(self, db_path: str = "data/workspaces.json"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        if not os.path.exists(self.db_path):
            self._write({"workspaces": []})

    def _read(self):
        with open(self.db_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data):
        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def create_workspace(self, user_id: str, domain: str, input_text: str | None = None):
        data = self._read()
        workspace = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "domain": domain,
            "input_text": input_text,
            "created_at": datetime.utcnow().isoformat(),
            "milestone": "baseline",
            "outputs": {},
        }
        data["workspaces"].append(workspace)
        self._write(data)
        return workspace

    def get_workspace(self, workspace_id: str):
        data = self._read()
        for workspace in data["workspaces"]:
            if workspace["id"] == workspace_id:
                return workspace
        raise ValueError(f"Workspace not found: {workspace_id}")

    def save_output(self, workspace_id: str, key: str, result: dict):
        data = self._read()
        for workspace in data["workspaces"]:
            if workspace["id"] == workspace_id:
                workspace["outputs"][key] = result
                self._write(data)
                return
        raise ValueError(f"Workspace not found: {workspace_id}")