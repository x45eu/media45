import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="MediaDetails",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("imdb_id", models.CharField(max_length=16)),
                ("tmdb_id", models.PositiveIntegerField()),
                ("synopsis", models.TextField(blank=True)),
                ("genres", models.JSONField(blank=True, default=list)),
                ("runtime_minutes", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("rating", models.DecimalField(blank=True, decimal_places=1, max_digits=3, null=True)),
                ("fetched_at", models.DateTimeField()),
                ("media", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="details", to="catalog.media")),
            ],
        ),
    ]
