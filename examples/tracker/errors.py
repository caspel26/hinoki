"""Business failures, named after the rule they report."""

from hinoki import errors


class TaskAlreadyDone(errors.Conflict):
    code = "task_already_done"
    message = "The task is already done."


class DoneTasksAreKept(errors.Conflict):
    code = "done_tasks_are_kept"
    message = "Completed tasks are kept as a record and cannot be deleted."
