"""What the Blog application serves (shared by ``main.py`` and the tests)."""

from hinoki import Includable, Mount

from .adminsite import site
from .api import Posts, health

routes: list[Includable] = [health, Posts, Mount("/admin", site)]
