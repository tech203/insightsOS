import json
import os
import uuid
from datetime import datetime


class JobTracker:
    def __init__(self, db_path: str = "data/jobs.json"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        if not os.path.exists(self.db_path):
            self._write({"jobs": []})

    def _read(self):
        with open(self.db_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data):
        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def create_job(self, workspace_id: str, action: str) -> str:
        data = self._read()
        job_id = str(uuid.uuid4())
        data["jobs"].append({
            "id": job_id,
            "workspace_id": workspace_id,
            "action": action,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "result": None,
            "error": None,
        })
        self._write(data)
        return job_id

    def update_status(self, job_id: str, status: str, result=None, error=None):
        data = self._read()
        for job in data["jobs"]:
            if job["id"] == job_id:
                job["status"] = status
                job["updated_at"] = datetime.utcnow().isoformat()
                if result is not None:
                    job["result"] = result
                if error is not None:
                    job["error"] = error
                self._write(data)
                return
        raise ValueError(f"Job not found: {job_id}")

    def get_job(self, job_id: str):
        data = self._read()
        for job in data["jobs"]:
            if job["id"] == job_id:
                return job
        raise ValueError(f"Job not found: {job_id}")