from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import ConvenioViewSet, redeem

router = SimpleRouter()
router.register("agreements", ConvenioViewSet, basename="agreement")

# redeem/ first: the router's detail routes only take numeric ids anyway.
urlpatterns = [
    path("agreements/redeem/", redeem, name="agreement-redeem"),
] + router.urls
