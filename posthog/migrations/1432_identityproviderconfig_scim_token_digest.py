from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "1431_squash_2026_11_02_schema_addons"),
    ]

    operations = [
        migrations.AddField(
            model_name="identityproviderconfig",
            name="scim_token_digest",
            field=models.CharField(blank=True, max_length=128, null=True, unique=True),
        ),
    ]
