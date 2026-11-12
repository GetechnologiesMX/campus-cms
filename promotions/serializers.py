from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q, Sum
from rest_framework import serializers

from catalog.models import Product
from sales.models import Order

from . import pricing
from .models import Promocion, code_validator, normalize_code


def report(promotion):
    """Uses and money of a promotion, for the contadores."""
    totals = promotion.orders.aggregate(
        paid=Count("pk", filter=Q(status=Order.Status.PAID)),
        refunded=Count("pk", filter=Q(status=Order.Status.REFUNDED)),
        revenue=Sum("amount", filter=Q(status=Order.Status.PAID)),
        discounts=Sum("discount", filter=Q(status=Order.Status.PAID)),
    )
    uses, _ = pricing.uses(promotion)
    return {
        # Paid + refunded + checkouts open right now.
        "uses": uses,
        "remaining": max(promotion.max_uses - uses, 0) if promotion.max_uses is not None else None,
        "paid": totals["paid"],
        "refunded": totals["refunded"],
        "revenue": str(totals["revenue"] or "0.00"),
        "discounts": str(totals["discounts"] or "0.00"),
    }


def state(promotion, uses=None):
    if not promotion.active:
        return "inactive"
    if promotion.has_ended():
        return "ended"
    if not promotion.has_started():
        return "scheduled"
    if promotion.max_uses is not None:
        if (uses if uses is not None else pricing.uses(promotion)[0]) >= promotion.max_uses:
            return "sold_out"
    return "running"


class PromotionProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "title", "course_key", "status", "price")


class PromocionSerializer(serializers.ModelSerializer):
    """A promotion for the contadores' admin, with its state and uses."""

    code = serializers.CharField(max_length=40, required=False, allow_blank=True, allow_null=True)
    products = serializers.PrimaryKeyRelatedField(many=True, queryset=Product.objects.all(), required=False)
    products_detail = PromotionProductSerializer(source="products", many=True, read_only=True)
    state = serializers.SerializerMethodField()
    report = serializers.SerializerMethodField()

    class Meta:
        model = Promocion
        fields = ("id", "name", "kind", "code", "discount_type", "value", "all_products", "products",
                  "products_detail", "starts_at", "ends_at", "max_uses", "max_uses_per_user", "active", "notes",
                  "state", "report", "created", "modified")
        read_only_fields = ("created", "modified")

    def get_report(self, promotion):
        return report(promotion)

    def get_state(self, promotion):
        return state(promotion)

    def validate_code(self, value):
        if not value or not value.strip():
            return None
        code = value.strip().upper()
        code_validator(code)
        code = normalize_code(code)
        taken = Promocion.objects.filter(code=code)
        if self.instance is not None:
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise serializers.ValidationError("Ya hay una promoción con este código.")
        return code

    def validate_products(self, products):
        bad = [p.title for p in products if p.status == Product.Status.ARCHIVED]
        if bad:
            raise serializers.ValidationError(f"Estos cursos están archivados: {', '.join(bad)}.")
        return products

    def validate(self, attrs):
        if self.instance is not None:
            if "kind" in attrs and attrs["kind"] != self.instance.kind:
                raise serializers.ValidationError({"kind": "El tipo no se cambia: crea otra promoción."})
            changes_discount = any(
                field in attrs and attrs[field] != getattr(self.instance, field) for field in ("discount_type", "value")
            )
            if changes_discount and self.instance.orders.exists():
                raise serializers.ValidationError(
                    {"value": "Ya se usó: el descuento no se cambia. Desactívala y crea otra."})
        merged = {field.name: getattr(self.instance, field.name) for field in Promocion._meta.concrete_fields
                  if self.instance is not None}
        merged.update({key: value for key, value in attrs.items() if key != "products"})
        if merged.get("kind") == Promocion.Kind.OFFER and "max_uses_per_user" not in attrs and self.instance is None:
            # An offer is for every course it covers: no per-person limit unless asked.
            attrs["max_uses_per_user"] = merged["max_uses_per_user"] = None
        candidate = Promocion(**{key: value for key, value in merged.items() if key != "id"})
        try:
            candidate.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        products = attrs.get("products")
        if products is None and self.instance is not None:
            products = list(self.instance.products.all())
        if not merged.get("all_products") and not products:
            raise serializers.ValidationError({"products": "Elige al menos un curso, o «todos los cursos»."})
        return attrs
