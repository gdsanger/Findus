from django.contrib.auth.models import AbstractUser
from django.db import models


class Department(models.Model):
    """Org-unit used as a visibility scope, not a hard tenant partition.

    See Architektur.md, "Sichtbarkeitsmodell": department = filter/scope
    (n:n with users), survives reorgs better than a rigid tenant split.
    """

    name = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class User(AbstractUser):
    departments = models.ManyToManyField(
        Department,
        related_name="members",
        blank=True,
    )

    def __str__(self):
        return self.get_username()
