"""Unmanaged models over the shared tables (Django never creates or migrates them)."""

import uuid

from django.db import models


class Author(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    name = models.CharField(max_length=80)

    class Meta:
        managed = False
        db_table = "authors"


class Post(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    author = models.ForeignKey(Author, on_delete=models.PROTECT, db_column="author_id")
    title = models.CharField(max_length=120)
    body = models.TextField()
    published = models.BooleanField(default=False)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "posts"
