"""
Promotions: discounts for anyone, on some courses or all of them.

- An *offer* applies by itself (a new course, a season sale): the catalog
  shows the course with its list price crossed out.
- A *discount code* applies when the buyer types it at checkout.

Prices are computed here, in the CMS (promotions.pricing), and the payment
gateway only receives the final amount, so another gateway can be added
without moving this logic. Company agreements (agreements app) are separate:
seats billed outside the platform.
"""
import re
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

from catalog.models import Product

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_-]{2,31}$"
code_validator = RegexValidator(
    CODE_PATTERN, "De 3 a 32 caracteres: letras sin acentos, números, guion o guion bajo (p. ej. BUENFIN25).",
)


def normalize_code(raw):
    """" buenfin25 " → "BUENFIN25". Returns "" if it can't be a code."""
    code = re.sub(r"\s+", "", str(raw or "")).upper()
    return code if re.match(CODE_PATTERN, code) else ""


class Promocion(models.Model):
    class Kind(models.TextChoices):
        OFFER = "offer", "Oferta (sin código)"
        CODE = "code", "Código de descuento"

    class DiscountType(models.TextChoices):
        PERCENT = "percent", "Porcentaje"
        AMOUNT = "amount", "Monto fijo (MXN)"

    name = models.CharField("nombre", max_length=120, help_text="Para identificarla, p. ej. «Buen Fin 2026».")
    kind = models.CharField("tipo", max_length=8, choices=Kind.choices)
    code = models.CharField(
        "código", max_length=32, unique=True, null=True, blank=True, validators=[code_validator],
        help_text="Solo para códigos de descuento. Se guarda en mayúsculas.",
    )
    discount_type = models.CharField("tipo de descuento", max_length=8, choices=DiscountType.choices)
    value = models.DecimalField(
        "descuento", max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Porcentaje (1 a 100) o pesos, según el tipo de descuento.",
    )
    all_products = models.BooleanField("todos los cursos", default=False)
    products = models.ManyToManyField(Product, verbose_name="cursos", blank=True, related_name="promotions")
    starts_at = models.DateTimeField("empieza", null=True, blank=True, help_text="Vacío: desde ya.")
    ends_at = models.DateTimeField("termina", null=True, blank=True, help_text="Vacío: sin fecha de fin.")
    max_uses = models.PositiveIntegerField(
        "usos en total", null=True, blank=True, validators=[MinValueValidator(1)],
        help_text="Vacío: sin límite. Una compra reembolsada sigue contando.",
    )
    max_uses_per_user = models.PositiveIntegerField(
        "usos por persona", null=True, blank=True, default=1, validators=[MinValueValidator(1)],
        help_text="En cuántos cursos puede usarla cada persona. Vacío: sin límite.",
    )
    active = models.BooleanField("activa", default=True)
    notes = models.TextField("notas", blank=True)
    created = models.DateTimeField("creada", auto_now_add=True)
    modified = models.DateTimeField("modificada", auto_now=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = "promoción"
        verbose_name_plural = "promociones"

    def __str__(self):
        return f"{self.name} ({self.code})" if self.code else self.name

    def clean(self):
        errors = {}
        if self.kind == self.Kind.CODE and not self.code:
            errors["code"] = "Escribe el código."
        if self.kind == self.Kind.OFFER and self.code:
            errors["code"] = "Una oferta no lleva código."
        if self.discount_type == self.DiscountType.PERCENT and self.value is not None:
            # A 100% offer would give every course away to anyone: only codes can.
            top = Decimal(100) if self.kind == self.Kind.CODE else Decimal("99.99")
            if self.value > top:
                errors["value"] = ("Un porcentaje va de 1 a 100." if self.kind == self.Kind.CODE else
                                   "Una oferta puede ser de hasta 99%: para regalar un curso, usa un código.")
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            errors["ends_at"] = "Termina antes de empezar."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.code = normalize_code(self.code) or None
        super().save(*args, **kwargs)

    def has_started(self, now=None):
        return self.starts_at is None or self.starts_at <= (now or timezone.now())

    def has_ended(self, now=None):
        return self.ends_at is not None and self.ends_at <= (now or timezone.now())

    def is_running(self, now=None):
        """Active and within its dates (uses are counted apart: see pricing.uses)."""
        return self.active and self.has_started(now) and not self.has_ended(now)

    def applies_to(self, product):
        return self.all_products or self.products.filter(pk=product.pk).exists()


class CodeAttempt(models.Model):
    """A discount code typed wrong, to limit guessing. Kept one day."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
                             related_name="+")
    ip = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "intento fallido de código"
        verbose_name_plural = "intentos fallidos de código"
