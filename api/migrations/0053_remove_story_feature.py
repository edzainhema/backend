from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0052_post_is_public_override'),
    ]

    operations = [
        # StorySeen has FK to Story, so delete it first.
        migrations.DeleteModel(
            name='StorySeen',
        ),
        migrations.DeleteModel(
            name='Story',
        ),
    ]
