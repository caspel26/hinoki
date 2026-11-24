"""Unmanaged models over the benchmark tables (Django never creates them)."""

from django.db import models


class BenchmarkUser(models.Model):
    username = models.CharField(max_length=50, unique=True)
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "benchmark_users"


class Author(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    bio = models.TextField()
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "articles_author"


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(max_length=50, unique=True)

    class Meta:
        managed = False
        db_table = "articles_tag"


class Article(models.Model):
    title = models.CharField(max_length=200)
    content = models.TextField()
    published = models.BooleanField(default=False)
    author = models.ForeignKey(
        Author, on_delete=models.CASCADE, related_name="articles"
    )
    tags = models.ManyToManyField(Tag, related_name="articles")
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "articles_article"
        ordering = ("-created_at",)


class Comment(models.Model):
    article = models.ForeignKey(
        Article, on_delete=models.CASCADE, related_name="comments"
    )
    author_name = models.CharField(max_length=100)
    content = models.TextField()
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "articles_comment"
        ordering = ("-created_at",)
