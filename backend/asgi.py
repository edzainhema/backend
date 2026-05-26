import os
from django.core.asgi import get_asgi_application

# 1. Set settings first
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

# 2. Initialize the Django ASGI application
# This "boots up" Django so that the App Registry is ready
django_asgi_app = get_asgi_application()

# 3. NOW import your custom app modules
# These MUST be imported after get_asgi_application()
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from api.jwt_middleware import JWTAuthMiddleware
from api.routing import websocket_urlpatterns

# 4. Define the final application router
application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        JWTAuthMiddleware(
            URLRouter(websocket_urlpatterns)
        )
    ),
})
