from django.contrib import admin

from .models import CodeAttempt, Promocion


@admin.register(Promocion)
class PromocionAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "code", "discount_type", "value", "starts_at", "ends_at", "max_uses", "active")
    list_filter = ("kind", "active")
    search_fields = ("name", "code")
    filter_horizontal = ("products",)
    readonly_fields = ("created", "modified")


@admin.register(CodeAttempt)
class CodeAttemptAdmin(admin.ModelAdmin):
    list_display = ("user", "ip", "created")
    readonly_fields = ("user", "ip", "created")

    def has_add_permission(self, request):
        return False
