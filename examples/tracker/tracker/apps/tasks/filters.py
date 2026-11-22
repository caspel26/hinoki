"""What a client may ask of the task list. Used by the generated CRUD list."""

from datetime import datetime

from hinoki.filters import Eq, FilterSet, In, Range, Search


class TaskFilters(FilterSet):
    done: Eq[bool]
    priority: In[int]
    created_at: Range[datetime]
    completed_at: Eq[datetime | None]
    search = Search("title")
    ordering = ("created_at", "priority", "title")
