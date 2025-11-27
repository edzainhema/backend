from rest_framework import serializers
from .models import Media

class MediaSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()
	class Meta:
		model = Media
		fields = ['id', 'user', 'file', 'file_url', 'uploaded_at']
	
	def get_file_url(self, obj):
		request = self.context.get('request')
		if request:
			return request.build_absolute_uri(obj.file.url)
		return obj.file.url
