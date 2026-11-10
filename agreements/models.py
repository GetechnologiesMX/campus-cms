"""
Company agreements: a company buys N seats for its employees, outside the
platform (invoice or payment link), and each employee redeems a single-use
code that enrolls them in an included course without going through Stripe.
"""
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

from catalog.models import Product

# No 0/O, 1/I/L: codes are read aloud, copied from a PDF, typed on a phone.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
RANDOM_LENGTH = 8  # 31^8 ≈ 8.5e11 combinations per prefix
# Products a code can enroll in: they exist in Open edX and aren't archived.
# "created" is allowed: an agreement may include a course not sold to the public.
REDEEMABLE_STATUSES = (Product.Status.CREATED, Product.Status.ON_SALE)

prefix_validator = RegexValidator(
    rf"^[{CODE_ALPHABET}]{{2,6}}$",
    f"De 2 a 6 caracteres, solo mayúsculas y números sin 0, 1, O, I ni L ({CODE_ALPHABET}).",
)


def normalize_code(raw):
    """
    "acme 7k3p-q9xd" → "ACME-7K3P-Q9XD": uppercase, no spaces, hyphens where
    the generator puts them (the last 8 characters are the random part).
    Returns None if it can't be a code.
    """
    compact = re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())
    prefix, random_part = compact[:-RANDOM_LENGTH], compact[-RANDOM_LENGTH:]
    if not 2 <= len(prefix) <= 6 or len(random_part) != RANDOM_LENGTH:
        return None
    return f"{prefix}-{random_part[:4]}-{random_part[4:]}"


class Convenio(models.Model):
    company = models.CharField("empresa", max_length=255)
    contact_email = models.EmailField("correo de contacto")
    products = models.ManyToManyField(
        Product, verbose_name="cursos incluidos", related_name="convenios",
        limit_choices_to={"status__in": REDEEMABLE_STATUSES},
        help_text="Cursos creados en Open edX o a la venta. Uno no publicado al público también sirve.",
    )
    seats = models.PositiveIntegerField(
        "lugares", validators=[MinValueValidator(1)],
        help_text="Lugares comprados. Los códigos anulados sin usar y aquellos a los que se les quitó el acceso no "
                  "cuentan, así que se pueden reponer.",
    )
    code_prefix = models.CharField(
        "prefijo de los códigos", max_length=6, validators=[prefix_validator],
        help_text="Aparece al inicio de cada código, p. ej. ACME → ACME-7K3P-Q9XD.",
    )
    expires_at = models.DateField(
        "vence el",
        help_text="Último día en que se pueden canjear códigos. Que venza no quita el acceso a quien ya canjeó "
                  "(para eso, usa «Quitar acceso» en sus códigos).",
    )
    active = models.BooleanField(
        "activo", default=True,
        help_text="Desactivarlo impide nuevos canjes, pero no quita el acceso a quien ya canjeó un código (para eso, "
                  "usa «Quitar acceso» en sus códigos).",
    )
    notes = models.TextField("notas", blank=True)
    created = models.DateTimeField("creado", auto_now_add=True)
    modified = models.DateTimeField("modificado", auto_now=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = "convenio"
        verbose_name_plural = "convenios"

    def __str__(self):
        return self.company

    def is_current(self):
        """Active and not past its last day (in the CMS time zone)."""
        return self.active and timezone.localdate() <= self.expires_at

    def seats_taken(self):
        """
        Codes that count against the seats: every one except those voided
        before anyone used them and those whose access was removed (a voided
        *used* code still holds a seat: whoever redeemed it keeps the access).
        """
        return (self.codes.exclude(status=CodigoConvenio.Status.VOID, redeemed_at__isnull=True)
                .exclude(status=CodigoConvenio.Status.REVOKED).count())


class CodigoConvenio(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "available", "Disponible"
        USED = "used", "Usado"
        VOID = "void", "Anulado"
        # Someone redeemed it and a contador removed their access later. It
        # no longer holds a seat: a new code can be generated in its place.
        REVOKED = "revoked", "Acceso retirado"

    code = models.CharField("código", max_length=32, unique=True)
    convenio = models.ForeignKey(Convenio, on_delete=models.PROTECT, related_name="codes")
    status = models.CharField(
        "estado", max_length=16, choices=Status.choices, default=Status.AVAILABLE, db_index=True,
        help_text="Anular un código ya usado no quita el acceso a quien lo canjeó; «Quitar acceso» sí.",
    )
    redeemed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="+", verbose_name="canjeado por")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
                                verbose_name="curso en que se usó")
    redeemed_at = models.DateTimeField("canjeado el", null=True, blank=True)
    # The amount-0 order that records the enrollment (Order.origin = agreement).
    order = models.OneToOneField("sales.Order", on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="agreement_code", verbose_name="orden")
    revoked_at = models.DateTimeField("acceso retirado el", null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="+", verbose_name="acceso retirado por")
    created = models.DateTimeField("creado", auto_now_add=True)

    class Meta:
        ordering = ["created", "pk"]
        verbose_name = "código de convenio"
        verbose_name_plural = "códigos de convenio"

    def __str__(self):
        return self.code

    def clean(self):
        normalized = normalize_code(self.code)
        if normalized is None:
            raise ValidationError({"code": "No tiene el formato de un código (PREFIJO-XXXX-XXXX)."})
        self.code = normalized


class RedeemAttempt(models.Model):
    """
    A failed redeem, to limit guessing (see services.check_rate_limit). Rows
    older than a day are deleted when new ones are added, and by the
    purge_redeem_attempts command.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    ip = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "intento fallido de canje"
        verbose_name_plural = "intentos fallidos de canje"
