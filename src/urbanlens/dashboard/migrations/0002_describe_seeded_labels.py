"""Give the labels every profile was seeded with the descriptions new profiles now get, where none was written."""

from django.db import migrations
from django.db.models import Q

#: (kind, name, description), frozen from ``models/labels/signals.py`` as of this migration.
SEEDED_DESCRIPTIONS = [
    ("status", "Visited", "Places you have been."),
    ("status", "Want to Go", "Places you would like to visit."),
    ("status", "Active", "Still in use, occupied or open."),
    ("status", "Abandoned", "Disused and left empty."),
    ("status", "Demolished", "Torn down or otherwise gone."),
    ("category", "Amusement Park", "Theme parks, fairgrounds and water parks, with their rides and midways."),
    ("category", "Asylum", "Psychiatric hospitals, state schools and sanatoriums."),
    ("category", "Bank", "Banks, vaults and old financial buildings."),
    ("category", "Bridge", "Road, rail and foot bridges, trestles and viaducts."),
    ("category", "Cave", "Natural caves, grottoes and mines."),
    ("category", "Castle", "Castles, forts and fortified houses."),
    ("category", "Church", "Churches, chapels, temples and other places of worship."),
    ("category", "Factory", "Mills, plants, foundries, warehouses and other industrial sites."),
    ("category", "Fire Tower", "Fire lookout towers and other watch towers."),
    ("category", "Graveyard", "Cemeteries, graveyards and mausoleums."),
    ("category", "Hospital", "Hospitals, clinics and medical centres."),
    ("category", "Hotel", "Hotels, motels, inns and lodges."),
    ("category", "House", "Homes, farmhouses and other residences."),
    ("category", "Laboratory", "Research labs, test facilities and observatories."),
    ("category", "Mall", "Shopping malls, department stores and retail plazas."),
    ("category", "Mansion", "Estates, manors and other grand houses."),
    ("category", "Monument", "Memorials, statues and landmark structures."),
    ("category", "Morgue", "Morgues, funeral homes and crematoriums."),
    ("category", "Park", "Parks, gardens and other outdoor public spaces."),
    ("category", "Parking", "Places to park nearby, or parking structures worth visiting."),
    ("category", "Power Plant", "Power stations, substations, dams and other utilities."),
    ("category", "Prison", "Prisons, jails, reformatories and detention centres."),
    ("category", "Resort", "Resorts, spas, camps and holiday complexes."),
    ("category", "Ruins", "Remains of a structure, from foundations to standing shells."),
    ("category", "School", "Schools, colleges and universities."),
    ("category", "Stadium", "Stadiums, arenas, racetracks and sports grounds."),
    ("category", "Theater", "Theatres, cinemas, opera houses and music halls."),
    ("category", "Train Station", "Railway stations, depots, yards and roundhouses."),
    ("category", "Tunnel", "Tunnels, drains, sewers and other underground passages."),
    ("tag", "Notable", "Somewhere worth a special trip."),
    ("tag", "Graffiti", "Known for its graffiti or street art."),
    ("tag", "Photography", "Good for photographs."),
    ("tag", "Dangerous", "Has real hazards: unstable structure, hazardous materials, or other risks."),
    ("tag", "Popular", "Busy and well known, so expect other visitors."),
    ("user", "Preservation", "People working to preserve or document places."),
    ("user", "Vandalism", "People known to damage or deface places."),
    ("user", "Photography", "Photographers."),
    ("user", "Influencer", "People with a public following who may draw crowds to a place."),
    ("media", "Interior", "Taken inside."),
    ("media", "Exterior", "Taken outside."),
    ("media", "Document", "Scans of documents, plans, signs or papers."),
    ("media", "Video", "Video footage."),
]


def describe_seeded_labels(apps, schema_editor):
    Label = apps.get_model("dashboard", "Label")
    for kind, name, description in SEEDED_DESCRIPTIONS:
        Label.objects.filter(Q(description__isnull=True) | Q(description=""), kind=kind, name__iexact=name).update(description=description)


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0001_initial")]

    operations = [migrations.RunPython(describe_seeded_labels, migrations.RunPython.noop)]
