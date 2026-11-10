from rest_framework import serializers

from catalog.models import Product

from . import services
from .models import REDEEMABLE_STATUSES, CodigoConvenio, Convenio, prefix_validator


class AgreementProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "title", "course_key", "status")


class ConvenioSerializer(serializers.ModelSerializer):
    """An agreement for the contadores' admin, with its seats and code counts."""

    products = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Product.objects.all(), allow_empty=False,
        error_messages={"empty": "Incluye al menos un curso."},
    )
    products_detail = AgreementProductSerializer(source="products", many=True, read_only=True)
    code_prefix = serializers.CharField(max_length=6)
    report = serializers.SerializerMethodField()
    is_current = serializers.SerializerMethodField()

    class Meta:
        model = Convenio
        fields = ("id", "company", "contact_email", "products", "products_detail", "seats", "code_prefix",
                  "expires_at", "active", "notes", "is_current", "report", "created", "modified")
        read_only_fields = ("created", "modified")

    def get_report(self, convenio):
        return services.report(convenio)

    def get_is_current(self, convenio):
        return convenio.is_current()

    def validate_code_prefix(self, value):
        value = value.strip().upper()
        prefix_validator(value)
        return value

    def validate_products(self, products):
        bad = [p.title for p in products if p.status not in REDEEMABLE_STATUSES or not p.course_key]
        if bad:
            raise serializers.ValidationError(f"Solo cursos creados en Open edX o a la venta: {', '.join(bad)}.")
        return products

    def validate_seats(self, seats):
        if seats < 1:
            raise serializers.ValidationError("Debe tener al menos un lugar.")
        taken = self.instance.seats_taken() if self.instance is not None else 0
        if seats < taken:
            raise serializers.ValidationError(f"Ya hay {taken} códigos que ocupan lugar; anula los que sobren primero.")
        return seats


class CodigoConvenioSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="redeemed_by.username", default=None, read_only=True)
    email = serializers.CharField(source="redeemed_by.email", default=None, read_only=True)
    course_key = serializers.CharField(source="product.course_key", default=None, read_only=True)
    course_title = serializers.CharField(source="product.title", default=None, read_only=True)
    # The access in Open edX of whoever redeemed it (Order.access), null if unused.
    access = serializers.CharField(source="order.access", default=None, read_only=True)
    revoked_by = serializers.CharField(source="revoked_by.username", default=None, read_only=True)

    class Meta:
        model = CodigoConvenio
        fields = ("id", "code", "status", "username", "email", "course_key", "course_title", "redeemed_at", "access",
                  "revoked_at", "revoked_by", "created")
        read_only_fields = fields
