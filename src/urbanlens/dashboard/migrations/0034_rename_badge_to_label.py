# Renames the Badge concept to Label throughout the schema (see CLAUDE.md / models/labels/).
#
# RenameModel + AlterModelTable follows the same idiom the team used for the
# Campus -> Boundary rename attempt in migrations/old/0002_v0_4_0b0.py, except here
# the schema is unchanged so a straight rename (not a create-new/backfill/drop) applies.

import django.db.models.deletion
from django.db import migrations, models


def rename_permissions(apps, schema_editor):
    """Rename auth_permission codenames/names in place so existing Group/User grants survive.

    Django's post_migrate `create_permissions` hook only ever creates permissions for
    codenames declared on the current model state - it does not delete or rename ones
    left behind by a renamed model, so without this the old `*_badge*` permission rows
    would linger as orphans instead of becoming the new `*_label*` ones.
    """
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    # (old content-type model name before this migration's RenameModel ops ran / after,
    # since ContentType.model is auto-updated by Django's RenameModel contenttypes hook)
    renames = [
        ("label", "add_badge", "add_label", "Can add label"),
        ("label", "change_badge", "change_label", "Can change label"),
        ("label", "delete_badge", "delete_label", "Can delete label"),
        ("label", "view_badge", "view_label", "Can view label"),
        ("label", "edit_global_badge", "edit_global_label", "Can edit global labels"),
        ("labelcustomization", "add_badgecustomization", "add_labelcustomization", "Can add label customization"),
        ("labelcustomization", "change_badgecustomization", "change_labelcustomization", "Can change label customization"),
        ("labelcustomization", "delete_badgecustomization", "delete_labelcustomization", "Can delete label customization"),
        ("labelcustomization", "view_badgecustomization", "view_labelcustomization", "Can view label customization"),
        ("profilelabelassignment", "add_profilebadgeassignment", "add_profilelabelassignment", "Can add profile label assignment"),
        ("profilelabelassignment", "change_profilebadgeassignment", "change_profilelabelassignment", "Can change profile label assignment"),
        ("profilelabelassignment", "delete_profilebadgeassignment", "delete_profilelabelassignment", "Can delete profile label assignment"),
        ("profilelabelassignment", "view_profilebadgeassignment", "view_profilelabelassignment", "Can view profile label assignment"),
    ]
    for model_name, old_codename, new_codename, new_name in renames:
        try:
            content_type = ContentType.objects.get(app_label="dashboard", model=model_name)
        except ContentType.DoesNotExist:
            continue
        Permission.objects.filter(content_type=content_type, codename=old_codename).update(codename=new_codename, name=new_name)


def rename_permissions_reverse(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    renames = [
        ("badge", "add_label", "add_badge", "Can add badge"),
        ("badge", "change_label", "change_badge", "Can change badge"),
        ("badge", "delete_label", "delete_badge", "Can delete badge"),
        ("badge", "view_label", "view_badge", "Can view badge"),
        ("badge", "edit_global_label", "edit_global_badge", "Can edit global badges"),
        ("badgecustomization", "add_labelcustomization", "add_badgecustomization", "Can add badge customization"),
        ("badgecustomization", "change_labelcustomization", "change_badgecustomization", "Can change badge customization"),
        ("badgecustomization", "delete_labelcustomization", "delete_badgecustomization", "Can delete badge customization"),
        ("badgecustomization", "view_labelcustomization", "view_badgecustomization", "Can view badge customization"),
        ("profilebadgeassignment", "add_profilelabelassignment", "add_profilebadgeassignment", "Can add profile badge assignment"),
        ("profilebadgeassignment", "change_profilelabelassignment", "change_profilebadgeassignment", "Can change profile badge assignment"),
        ("profilebadgeassignment", "delete_profilelabelassignment", "delete_profilebadgeassignment", "Can delete profile badge assignment"),
        ("profilebadgeassignment", "view_profilelabelassignment", "view_profilebadgeassignment", "Can view profile badge assignment"),
    ]
    for model_name, old_codename, new_codename, new_name in renames:
        try:
            content_type = ContentType.objects.get(app_label="dashboard", model=model_name)
        except ContentType.DoesNotExist:
            continue
        Permission.objects.filter(content_type=content_type, codename=old_codename).update(codename=new_codename, name=new_name)


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0033_markupmap_inferred_pins"),
    ]

    operations = [
        # --- Badge -> Label ---
        migrations.RenameModel(old_name="Badge", new_name="Label"),
        migrations.AlterModelTable(name="label", table="dashboard_labels"),
        migrations.RenameIndex(model_name="label", old_name="idxdb_badge_uuid", new_name="idxdb_label_uuid"),
        migrations.RenameIndex(model_name="label", old_name="idxdb_badge_profile", new_name="idxdb_label_profile"),
        migrations.RenameIndex(model_name="label", old_name="idxdb_badge_pfile_ord", new_name="idxdb_label_pfile_ord"),
        migrations.AlterField(
            model_name="label",
            name="custom_icon",
            field=models.ImageField(blank=True, null=True, upload_to="label_icons/"),
        ),
        migrations.AlterField(
            model_name="label",
            name="profile",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="custom_labels",
                to="dashboard.profile",
            ),
        ),
        migrations.AlterModelOptions(
            name="label",
            options={"get_latest_by": "updated", "ordering": ["-order", "name"], "permissions": [("edit_global_label", "Can edit global labels")]},
        ),
        # --- BadgeCustomization -> LabelCustomization ---
        migrations.RenameModel(old_name="BadgeCustomization", new_name="LabelCustomization"),
        migrations.AlterModelTable(name="labelcustomization", table="dashboard_label_customizations"),
        migrations.RenameField(model_name="labelcustomization", old_name="badge", new_name="label"),
        migrations.AlterField(
            model_name="labelcustomization",
            name="label",
            field=models.ForeignKey(
                db_column="label_id",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="customizations",
                to="dashboard.label",
            ),
        ),
        migrations.AlterField(
            model_name="labelcustomization",
            name="profile",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="label_customizations", to="dashboard.profile"),
        ),
        migrations.RemoveConstraint(model_name="labelcustomization", name="unique_tag_customization_per_profile"),
        migrations.AddConstraint(
            model_name="labelcustomization",
            constraint=models.UniqueConstraint(fields=("profile", "label"), name="unique_label_customization_per_profile"),
        ),
        # --- ProfileBadgeAssignment -> ProfileLabelAssignment ---
        migrations.RenameModel(old_name="ProfileBadgeAssignment", new_name="ProfileLabelAssignment"),
        migrations.AlterModelTable(name="profilelabelassignment", table="dashboard_profile_label_assignments"),
        migrations.RenameField(model_name="profilelabelassignment", old_name="badge", new_name="label"),
        migrations.AlterField(
            model_name="profilelabelassignment",
            name="label",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="profile_assignments", to="dashboard.label"),
        ),
        migrations.AlterField(
            model_name="profilelabelassignment",
            name="author",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="profile_label_assignments", to="dashboard.profile"),
        ),
        migrations.AlterField(
            model_name="profilelabelassignment",
            name="subject",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="received_profile_label_assignments", to="dashboard.profile"),
        ),
        migrations.RemoveConstraint(model_name="profilelabelassignment", name="unique_profile_badge_assignment"),
        migrations.AddConstraint(
            model_name="profilelabelassignment",
            constraint=models.UniqueConstraint(fields=("author", "subject", "label"), name="unique_profile_label_assignment"),
        ),
        # --- Pin.badges / Wiki.badges M2M ---
        migrations.RenameField(model_name="pin", old_name="badges", new_name="labels"),
        migrations.AlterField(
            model_name="pin",
            name="labels",
            field=models.ManyToManyField(blank=True, related_name="pins", to="dashboard.label"),
        ),
        migrations.RenameField(model_name="wiki", old_name="badges", new_name="labels"),
        migrations.AlterField(
            model_name="wiki",
            name="labels",
            field=models.ManyToManyField(blank=True, related_name="wikis", to="dashboard.label"),
        ),
        # --- Profile.ai_badge_* -> Profile.ai_label_* ---
        migrations.RenameField(model_name="profile", old_name="ai_badge_tags", new_name="ai_label_tags"),
        migrations.RenameField(model_name="profile", old_name="ai_badge_categories", new_name="ai_label_categories"),
        migrations.RenameField(model_name="profile", old_name="ai_badge_statuses", new_name="ai_label_statuses"),
        # --- Stale auth_permission rows left behind by the renames above ---
        migrations.RunPython(rename_permissions, rename_permissions_reverse),
    ]
