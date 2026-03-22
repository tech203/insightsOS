from workspace_manager import WorkspaceManager
from credits import CreditLedger
from job_tracker import JobTracker
from result_normalizer import normalize_result

# import your existing agents
from project_setup_agent import run as run_project_setup
from query_agent import run as run_queries_agent
from visibility_agent import run as run_visibility_agent
from audit_runner import run as run_audit_agent
from content_brief_generator import run as run_brief_agent
from content_draft_generator import run as run_draft_agent


ACTION_COSTS = {
    "setup_workspace": 0,
    "run_queries": 0,
    "run_visibility": 0,
    "run_audit": 0,
    "generate_brief": 0,
    "generate_draft": 0,
}


class Orchestrator:
    def __init__(self):
        self.workspaces = WorkspaceManager()
        self.credits = CreditLedger()
        self.jobs = JobTracker()

    def _run_action(self, workspace_id: str, user_id: str, action: str, runner):
        cost = ACTION_COSTS.get(action, 0)

        if cost > 0 and not self.credits.has_enough(user_id, workspace_id, cost):
            return {
                "success": False,
                "error": "Not enough credits",
                "action": action,
                "credits_required": cost,
            }

        job_id = self.jobs.create_job(workspace_id, action)

        try:
            self.jobs.update_status(job_id, "running")

            raw_result = runner()
            result = normalize_result(action, raw_result)

            self.workspaces.save_output(workspace_id, action, result)

            if cost > 0:
                self.credits.deduct(user_id, workspace_id, cost, action)

            self.jobs.update_status(job_id, "completed", result=result)

            return {
                "success": True,
                "job_id": job_id,
                "workspace_id": workspace_id,
                "action": action,
                "result": result,
            }

        except Exception as e:
            self.jobs.update_status(job_id, "failed", error=str(e))
            return {
                "success": False,
                "job_id": job_id,
                "workspace_id": workspace_id,
                "action": action,
                "error": str(e),
            }

    def setup_workspace(self, user_id: str, domain: str, input_text: str | None = None):
        workspace = self.workspaces.create_workspace(user_id=user_id, domain=domain, input_text=input_text)

        return self._run_action(
            workspace_id=workspace["id"],
            user_id=user_id,
            action="setup_workspace",
            runner=lambda: run_project_setup(domain=domain, input_text=input_text),
        )

    def run_queries(self, user_id: str, workspace_id: str):
        workspace = self.workspaces.get_workspace(workspace_id)

        return self._run_action(
            workspace_id=workspace_id,
            user_id=user_id,
            action="run_queries",
            runner=lambda: run_queries_agent(workspace),
        )

    def run_visibility(self, user_id: str, workspace_id: str):
        workspace = self.workspaces.get_workspace(workspace_id)

        return self._run_action(
            workspace_id=workspace_id,
            user_id=user_id,
            action="run_visibility",
            runner=lambda: run_visibility_agent(workspace),
        )

    def run_audit(self, user_id: str, workspace_id: str):
        workspace = self.workspaces.get_workspace(workspace_id)

        return self._run_action(
            workspace_id=workspace_id,
            user_id=user_id,
            action="run_audit",
            runner=lambda: run_audit_agent(workspace),
        )

    def generate_brief(self, user_id: str, workspace_id: str, topic: str):
        workspace = self.workspaces.get_workspace(workspace_id)

        return self._run_action(
            workspace_id=workspace_id,
            user_id=user_id,
            action="generate_brief",
            runner=lambda: run_brief_agent(workspace=workspace, topic=topic),
        )

    def generate_draft(self, user_id: str, workspace_id: str, topic: str):
        workspace = self.workspaces.get_workspace(workspace_id)

        return self._run_action(
            workspace_id=workspace_id,
            user_id=user_id,
            action="generate_draft",
            runner=lambda: run_draft_agent(workspace=workspace, topic=topic),
        )