from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.models import User
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.contrib.auth.hashers import make_password
from .models import Media
from .serializers import MediaSerializer

# JWT
from rest_framework_simplejwt.tokens import RefreshToken


def hello_world(request):
	return JsonResponse({"message": "Hello to this bitch ass World from Django!"})


@api_view(['POST'])
def register_user(request):
    username = request.data.get('username')
    password = request.data.get('password')

    if User.objects.filter(username=username).exists():
        return Response({"error": "Username already taken"}, status=400)

    user = User.objects.create(
        username=username,
        password=make_password(password)
    )

    return Response({"message": "User registered successfully"})


@api_view(['POST'])
def login_user(request):
    username = request.data.get('username')
    password = request.data.get('password')

    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return Response({"error": "Invalid username or password"}, status=400)

    if not user.check_password(password):
        return Response({"error": "Invalid username or password"}, status=400)

    refresh = RefreshToken.for_user(user)

    return Response({
        "refresh": str(refresh),
        "access": str(refresh.access_token)
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def profile(request):
	media_qs = Media.objects.filter(user=request.user)
	serializer = MediaSerializer(media_qs, many=True, context={'request': request})
	
	return Response({
		"username": request.user.username,
		"media": serializer.data
	})
	
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_media(request):
    uploaded_file = request.FILES.get('file')

    if not uploaded_file:
        return Response({"error": "No file provided"}, status=400)

    media_item = Media.objects.create(
        user=request.user,
        file=uploaded_file
    )

    return Response({
        "message": "File uploaded successfully",
        "file_url": request.build_absolute_uri(media_item.file.url)
    })



