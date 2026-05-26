# Adds the RecommendedAuthor model — the precomputed output of the nightly
# collaborative-filtering job (ACTIVITY_AND_FEED_AUDIT.md item B3).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0075_notinterested'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RecommendedAuthor',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('score', models.FloatField(default=0.0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('author', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='recommended_to', to=settings.AUTH_USER_MODEL,
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='author_recommendations', to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
        migrations.AddConstraint(
            model_name='recommendedauthor',
            constraint=models.UniqueConstraint(
                fields=('user', 'author'), name='uniq_recommended_author'
            ),
        ),
        migrations.AddIndex(
            model_name='recommendedauthor',
            index=models.Index(fields=['user', '-score'], name='recauthor_user_score_idx'),
        ),
    ]
