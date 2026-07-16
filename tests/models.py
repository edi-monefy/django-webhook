from django.db import models


class User(models.Model):
    name = models.CharField(max_length=30)
    email = models.EmailField()
    join_date = models.DateField()
    last_active = models.DateTimeField()


class Country(models.Model):
    name = models.CharField(max_length=30)


class ModelWithFileField(models.Model):
    """
        The FileField can't be encoded with JSON.
    https://github.com/danihodovic/django-webhook/issues/35
    """

    file = models.FileField()


class Tag(models.Model):
    name = models.CharField(max_length=30)


class Article(models.Model):
    """
    Exercises the complete-snapshot (auto_now/auto_now_add fields) and reachable
    many-to-many serialization.
    """

    title = models.CharField(max_length=100)
    tags = models.ManyToManyField(Tag, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
