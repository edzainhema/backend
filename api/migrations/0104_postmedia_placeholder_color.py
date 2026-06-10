from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0103_alter_pagereport_handled_by_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='postmedia',
            name='placeholder_color',
            field=models.CharField(blank=True, max_length=7, null=True),
        ),
    ]
