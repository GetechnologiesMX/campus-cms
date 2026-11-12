from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import PromocionViewSet, quote

router = SimpleRouter()
router.register("promotions", PromocionViewSet, basename="promotion")

urlpatterns = [
    path("promotions/quote/", quote, name="promotion-quote"),
] + router.urls
