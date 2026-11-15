"""Model owned by the workflow project's accounts app."""

from hinoki.orm import Model, field


class WorkflowAccount(Model, table="workflow_accounts"):
    id: int = field(primary_key=True)
