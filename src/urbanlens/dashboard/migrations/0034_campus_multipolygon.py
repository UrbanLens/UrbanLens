"""Convert Campus.polygon from Polygon to MultiPolygon.

Uses RunSQL with ST_Multi() so existing single-polygon rows are preserved
as MultiPolygon(single_polygon).  The reverse migration takes only the first
sub-polygon, which is lossy for campuses that have multiple polygons.
"""

import django.contrib.gis.db.models.fields
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0033_profile_default_map_view"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # Alter the PostGIS column type in-place using ST_Multi().
            database_operations=[
                migrations.RunSQL(
                    sql="""
                        ALTER TABLE dashboard_campuses
                        ALTER COLUMN polygon
                        TYPE geography(MultiPolygon, 4326)
                        USING CASE
                            WHEN polygon IS NULL THEN NULL
                            ELSE ST_Multi(polygon::geometry)::geography
                        END;
                    """,
                    reverse_sql="""
                        ALTER TABLE dashboard_campuses
                        ALTER COLUMN polygon
                        TYPE geography(Polygon, 4326)
                        USING CASE
                            WHEN polygon IS NULL THEN NULL
                            ELSE ST_GeometryN(polygon::geometry, 1)::geography
                        END;
                    """,
                ),
            ],
            # Tell Django's migration state that the Python field changed.
            state_operations=[
                migrations.AlterField(
                    model_name="campus",
                    name="polygon",
                    field=django.contrib.gis.db.models.fields.MultiPolygonField(
                        blank=True, geography=True, null=True, srid=4326,
                    ),
                ),
            ],
        ),
    ]
