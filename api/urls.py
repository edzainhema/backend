from django.urls import path
from .views import hello_world, register_user, login_user, profile, upload_media


urlpatterns = [
    path('hello/', hello_world),
    
	# Auth system
	path('auth/register/', register_user),
	path('auth/login/', login_user),
	path('auth/profile/', profile),
	path('auth/upload/', upload_media),
]
