from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Department, User


@admin.register(User)
class FindusUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + ((
        "Findus",
        {"fields": ("departments",)},
    ),)
    filter_horizontal = UserAdmin.filter_horizontal + ("departments",)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)
