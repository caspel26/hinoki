"""What the Blog application serves (shared by ``main.py`` and the tests)."""

from hinoki import Includable, Mount

from .apps.blog.adminsite import site
from .apps.blog.api import Posts, health

routes: list[Includable] = [health, Posts, Mount("/admin", site)]
