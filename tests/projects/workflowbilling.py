"""Model owned by the workflow project's billing app."""

from hinoki.orm import Model, OnDelete, field, foreign_key
from tests.projects.workflowaccounts import WorkflowAccount


class WorkflowInvoice(Model, table="workflow_invoices"):
    id: int = field(primary_key=True)
    account_id: int = foreign_key(WorkflowAccount, on_delete=OnDelete.CASCADE)
