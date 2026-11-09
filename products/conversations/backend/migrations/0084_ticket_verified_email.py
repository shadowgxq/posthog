from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0083_squash_2026_11_02_schema_addons"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="verified_email",
            field=models.EmailField(blank=True, max_length=254, null=True),
        ),
    ]
