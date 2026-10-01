# REData's building lookup marks every building on any survey that names one HRSH building as on the property

- **Status: OPEN as of 2026-10-01.** Found by UrbanLens's location integration suite on a v0.8.0 dev environment
  (`v080e2e`). The figures below are from the `parcel_buildings` cache that environment wrote from production REData
  (`https://redata.urbanlens.org`) at 2026-10-01 03:22 UTC.
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `../REData`.

## What UrbanLens received

`lookup_buildings` for the Hudson River State Hospital parcel (41.73328, -73.92812) returned about 300 records with
`is_on_property: true`, which UrbanLens lists in the pin's Buildings card and offers as child pins:

| `match_scope` | Located | CRIS survey (`SurveyName`) | Records |
|---|---|---|---|
| `parcel` | yes | | 77 |
| `survey_roster` | no | Danskammer Energy | 109 |
| `survey_roster` | no | Town of Poughkeepsie, Reconnaissance Survey Update, 2011 | 42 |
| `survey_roster` | no | Hudson River State Hospital/Hudson River Psychiatric Center | 29 |
| `survey_roster` | no | Vassar College | 20 |
| `survey_roster` | no | DEP ACES Architectural Survey, Newburgh | 17 |
| `survey_roster` | no | Town of Poughkeepsie Reconnaissance-Level Survey; Update Phase I (2009 report) | 6 |
| `survey_roster` | no | SOUTH AVENUE HISTORIC DISTRICT | 2 |

Only the HRSH survey's roster plausibly describes this property. The rest are surveys that list one HRSH building
among many others: a power-plant corridor, two town-wide reconnaissance surveys, another campus, a Newburgh survey.
Every building on them came back flagged on the property. One example, `cris:02714.000019`:

```
"name": "VASSAR COLLEGE: ROCKEFELLER HALL, 1897",
"match_scope": "survey_roster", "is_on_property": true, "on_survey_roster": true,
"latitude": null, "longitude": null, "geometry": null, "confidence": 0.5308,
"attributes": {"USNDetail": "RAYMOND AVE, POUGHKEEPSIE NY", "ParentUSNNum": "02714.000578",
               "SurveyName": "Town of Poughkeepsie, Reconnaissance Survey Update, 2011", ...}
```

Its `ParentUSNNum` is Vassar College's, not HRSH's. The records carry no coordinates, so UrbanLens cannot check them
against the parcel itself; its boundary test keeps any record it cannot place.

## Asked

A survey roster puts a building on a property only when the survey is about that property, or the building's
`ParentUSNNum` is the property's own USN. Could `survey_roster` matching be limited to one of those, or the other
records come back `is_on_property: false` like the sensitivity-zone records already do?

## Not asked

UrbanLens takes `is_on_property` as given and will not reimplement the roster rule. Until REData changes, the
Buildings card of a property on a broad survey lists buildings from elsewhere.
