
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.text import slugify

from sales.models import Order

from . import services
from .models import REDEEMABLE_STATUSES, CodigoConvenio, Convenio, RedeemAttempt

NO_REVOKE_NOTE = ("Anular un código ya usado, desactivar el convenio o dejar que venza no quita el acceso a quien "
                  "ya canjeó un código: solo impide canjes nuevos. Para quitarle el curso a alguien, usa la acción "
                  "«Quitar acceso» en sus códigos.")


class ConvenioForm(forms.ModelForm):
    class Meta:
        model = Convenio
        fields = "__all__"

    def clean_code_prefix(self):
        return self.cleaned_data["code_prefix"].strip().upper()

    def clean_products(self):
        products = self.cleaned_data["products"]
        bad = [p.title for p in products if p.status not in REDEEMABLE_STATUSES or not p.course_key]
        if bad:
            raise forms.ValidationError(f"Solo cursos creados en Open edX o a la venta: {', '.join(bad)}.")
        return products


class GenerateCodesForm(forms.Form):
    count = forms.IntegerField(label="¿Cuántos códigos?", min_value=1)


class RedeemedCodesInline(admin.TabularInline):
    """Who used the agreement's codes, and when."""

    model = CodigoConvenio
    verbose_name = "código canjeado"
    verbose_name_plural = "quién canjeó los códigos"
    fields = ("code", "redeemed_by", "user_email", "product", "redeemed_at", "status", "revoked_at")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = True

    def get_queryset(self, request):
        return super().get_queryset(request).filter(redeemed_at__isnull=False).select_related("redeemed_by", "product")

    @admin.display(description="correo")
    def user_email(self, code):
        return code.redeemed_by.email if code.redeemed_by else ""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


def codes_csv_response(convenios):
    convenios = list(convenios)
    name = slugify(convenios[0].company) if len(convenios) == 1 else "convenios"
    response = HttpResponse(services.codes_csv(convenios), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="codigos-{name}.csv"'
    return response


@admin.register(Convenio)
class ConvenioAdmin(admin.ModelAdmin):
    form = ConvenioForm
    list_display = ("company", "seats", "generated", "used", "available", "expires_at", "active")
    list_filter = ("active",)
    search_fields = ("company", "contact_email", "code_prefix")
    filter_horizontal = ("products",)
    readonly_fields = ("summary", "created", "modified")
    fieldsets = (
        (None, {"fields": ("company", "contact_email", "products", "seats", "code_prefix", "expires_at", "active",
                           "notes")}),
        ("Reporte", {"fields": ("summary",), "description": NO_REVOKE_NOTE}),
        (None, {"fields": ("created", "modified")}),
    )
    inlines = [RedeemedCodesInline]
    actions = ["generate_codes_action", "export_codes_csv"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            n_generated=Count("codes"),
            n_used=Count("codes", filter=Q(codes__redeemed_at__isnull=False)
                         & ~Q(codes__status=CodigoConvenio.Status.REVOKED)),
            n_available=Count("codes", filter=Q(codes__status=CodigoConvenio.Status.AVAILABLE)),
        )

    @admin.display(description="generados", ordering="n_generated")
    def generated(self, convenio):
        return convenio.n_generated

    @admin.display(description="usados", ordering="n_used")
    def used(self, convenio):
        return convenio.n_used

    @admin.display(description="disponibles", ordering="n_available")
    def available(self, convenio):
        return convenio.n_available

    @admin.display(description="resumen")
    def summary(self, convenio):
        if not convenio.pk:
            return "Guarda el convenio para generar códigos."
        codes = services.report(convenio)
        return format_html(
            "Lugares comprados: <b>{}</b> · generados: <b>{}</b> · usados: <b>{}</b> · disponibles: <b>{}</b> · "
            "anulados: {} · acceso retirado: {} · lugares sin código: {}<br>"
            '<a class="button" href="{}">Generar códigos</a> <a class="button" href="{}">Exportar CSV</a> '
            '<a href="{}">Ver todos los códigos</a>',
            convenio.seats, codes["generated"], codes["used"], codes["available"], codes["void"], codes["revoked"],
            codes["free_seats"],
            reverse("admin:agreements_convenio_generate", args=[convenio.pk]),
            reverse("admin:agreements_convenio_export", args=[convenio.pk]),
            reverse("admin:agreements_codigoconvenio_changelist") + f"?convenio__id__exact={convenio.pk}",
        )

    def get_urls(self):
        return [
            path("<int:pk>/generar-codigos/", self.admin_site.admin_view(self.generate_codes_view),
                 name="agreements_convenio_generate"),
            path("<int:pk>/exportar-codigos/", self.admin_site.admin_view(self.export_view),
                 name="agreements_convenio_export"),
        ] + super().get_urls()

    def generate_codes_view(self, request, pk):
        convenio = get_object_or_404(Convenio, pk=pk)
        if not self.has_change_permission(request, convenio):
            raise PermissionDenied
        free = max(convenio.seats - convenio.seats_taken(), 0)
        form = GenerateCodesForm(request.POST or None, initial={"count": free or None})
        if request.method == "POST" and form.is_valid():
            try:
                codes = services.generate_codes(convenio, form.cleaned_data["count"])
            except ValueError as exc:
                form.add_error("count", str(exc))
            else:
                self.message_user(request, f"Se generaron {len(codes)} códigos para {convenio}.", messages.SUCCESS)
                return redirect("admin:agreements_convenio_change", convenio.pk)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"Generar códigos: {convenio}",
            "convenio": convenio,
            "free": free,
            "form": form,
        }
        return TemplateResponse(request, "admin/agreements/convenio/generate_codes.html", context)

    def export_view(self, request, pk):
        convenio = get_object_or_404(Convenio, pk=pk)
        if not self.has_view_permission(request, convenio):
            raise PermissionDenied
        return codes_csv_response([convenio])

    @admin.action(description="Generar códigos (un convenio)")
    def generate_codes_action(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Selecciona un solo convenio para generar códigos.", messages.WARNING)
            return None
        return redirect("admin:agreements_convenio_generate", queryset.get().pk)

    @admin.action(description="Exportar códigos a CSV")
    def export_codes_csv(self, request, queryset):
        return codes_csv_response(queryset.order_by("pk"))


@admin.register(CodigoConvenio)
class CodigoConvenioAdmin(admin.ModelAdmin):
    list_display = ("code", "convenio", "status", "redeemed_by", "product", "redeemed_at", "revoked_at")
    list_filter = ("status", "convenio")
    search_fields = ("code", "redeemed_by__username", "redeemed_by__email", "convenio__company")
    list_select_related = ("convenio", "redeemed_by", "product")
    readonly_fields = ("code", "convenio", "status", "redeemed_by", "product", "redeemed_at", "order", "revoked_at",
                       "revoked_by", "created")
    actions = ["void_codes", "revoke_access"]

    def has_add_permission(self, request):
        return False  # generated from the agreement, never typed by hand

    def has_delete_permission(self, request, obj=None):
        return False  # void them instead: the history stays

    @admin.action(description="Anular códigos (no quita el acceso a quien ya canjeó)")
    def void_codes(self, request, queryset):
        voided = services.void_codes(queryset)
        self.message_user(request, f"{voided} códigos anulados. {NO_REVOKE_NOTE}", messages.SUCCESS)

    @admin.action(description="Quitar acceso al curso a quien canjeó el código")
    def revoke_access(self, request, queryset):
        orders = services.revoke_access(queryset, by=request.user)
        pending = sum(order.access == Order.Access.ERROR for order in orders)
        self.message_user(request, f"Se quitó el acceso a {len(orders)} alumnos; sus lugares quedan libres.",
                          messages.SUCCESS)
        if pending:
            self.message_user(request, f"Open edX no respondió para {pending}: se reintentará con reconcile_orders "
                              "o con «Reintentar» en Ventas.", messages.WARNING)


@admin.register(RedeemAttempt)
class RedeemAttemptAdmin(admin.ModelAdmin):
    list_display = ("user", "ip", "created")
    search_fields = ("user__username", "ip")
    readonly_fields = ("user", "ip", "created")

    def has_add_permission(self, request):
        return False
