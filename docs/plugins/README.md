# Writing UrbanLens plugins

Features and integrations an install may not want are plugins: third-party data sources, paid services, hosted
infrastructure. Core never names a plugin. It declares **seams** and reads whatever plugins contribute to them,
so adding or removing a plugin needs no change to core.

## Two shapes

**Module plugin**: one module defining an `UrbanLensPlugin` subclass. Suits an integration that only contributes
providers, panels or rate-limit defaults. The bundled ones live in `src/urbanlens/dashboard/plugins/builtin/`.

**Package plugin**: a package whose `plugin.py` defines the plugin class. The package is also installed as a Django
app labelled with the plugin's name, so it can carry everything an app can:

```
src/urbanlens/plugins/storefront/
    __init__.py
    apps.py            # optional; an AppConfig with label = "storefront"
    plugin.py          # class StorefrontPlugin(UrbanLensPlugin): name = "storefront"
    models.py          # or models/; tables are the app's own
    migrations/
    admin.py           # found by Django's admin autodiscovery
    tasks.py           # found by Celery's autodiscovery
    urls.py
    views.py
    templates/storefront/
    static/storefront/
    frontend/ts/entries/   # bundled to static/storefront/js/ by `bun run build`
    frontend/sass/         # each non-partial .scss compiled to static/storefront/ by `bun run sass`
    management/commands/
    requirements.txt       # optional; see Dependencies
    build.sh               # optional; run in the image build after its requirements are installed
    integration/           # optional; Playwright routes and specs for the integration suite
    tests/
```

A package plugin's name must be its package name.

## Where plugins are found

In precedence order (the first plugin to claim a name wins):

1. Each subpackage of `urbanlens.plugins` (`src/urbanlens/plugins/`). Adding a directory adds the feature.
2. Each module of `urbanlens.dashboard.plugins.builtin`.
3. `UL_PLUGIN_MODULES`: comma-separated dotted paths of further modules or packages.
4. The `urbanlens.plugins` entry-point group of installed distributions, where the entry-point name is the plugin name:

   ```toml
   [project.entry-points."urbanlens.plugins"]
   storefront = "urbanlens_storefront"
   ```

`UL_DISABLED_PLUGINS` (comma-separated names) disables plugins. A disabled plugin is still listed on the site-admin
Plugins page. It contributes nothing, and its package is not installed as an app, but its tables and rows are left as
they are.

## Contributing

Override `setup()` and contribute through the context it receives. Every seam is declared in
`urbanlens.dashboard.plugins.points`. Read that module for the full list and what each seam expects.

```python
from django.urls import include, path

from urbanlens.dashboard.plugins import UrbanLensPlugin


class StorefrontPlugin(UrbanLensPlugin):
    name = "storefront"
    verbose_name = "Storefront"
    order = 50  # default priority of this plugin's contributions; lower comes first

    def setup(self, plugin):
        from urbanlens.dashboard.plugins import points

        plugin.add(points.ROOT_URLS, path("", include("urbanlens.plugins.storefront.urls")))
        plugin.add(points.PANEL_SOURCES, StorefrontPanelSource())
        plugin.register(points.SERVICE_DEFAULTS, "storefront", ServiceDefaults(...))
        plugin.filter(points.SUBSCRIPTION_ROLES, roles_bought_in_the_store)
        plugin.template(points.SLOT_SETTINGS_SECTIONS, "storefront/settings_section.html")
```

`setup()` runs once at startup and must not touch the database. Import heavy modules inside it, not at the top of
`plugin.py`.

A stylesheet or script a plugin builds is loaded on every page once it is added to `points.STYLESHEETS` or
`points.SCRIPTS` by its static path, e.g. `plugin.add(points.STYLESHEETS, "storefront/storefront.css")`.

### External services

Every external service a plugin calls needs rate-limit defaults on `points.SERVICE_DEFAULTS`, including an
`EgressCategory` that says which environments may call it: `quota` and `billed` services spend a share of a budget,
`deployed` ones are called by production and staging only, and `internal` ones are the deployment's own. A plugin whose
service depends on another it calls (a feature and the provider behind it) can refine the decision through
`points.EGRESS_DECISIONS`.

There are six kinds of seam:

| Kind | Contribute with | Core reads it as |
| --- | --- | --- |
| `ExtensionPoint` | `plugin.add(point, value)` | an ordered list: providers, panels, routes |
| `Registry` | `plugin.register(registry, key, value)` | a dict; the highest-priority claim on a key wins |
| `Choices` | `plugin.register(choices, value, label)` | a vocabulary of stored values, such as a model field's choices |
| `FilterHook` | `plugin.filter(hook, callback)` | a value passed through each callback |
| `ActionHook` | `plugin.on(hook, callback)` | a notification |
| `TemplateSlot` | `plugin.template(slot, "app/fragment.html")` | `{% plugin_slot "name" %}` in a core template |

### Priority

Contributions are ordered by priority, and lower priorities come first. A contribution that names no priority takes
the plugin's `order`, so one plugin can be preferred wholesale. A data source registered at priority 10 is asked
before the bundled public sources at 100. To override a single contribution, pass `priority=` to it.

### Errors

A plugin that fails to import or set up is logged and left inactive, and whatever it had contributed is withdrawn.
A failing filter or action callback is logged and skipped, so one broken plugin cannot break a request. The exception
is a hook declared with `on_error="raise"`: where skipping a callback would be unsafe, such as one that narrows what
a viewer may see, the error propagates.

### Requirements

Set `requires = ("other_plugin",)` when a plugin needs another. If any requirement is missing or inactive, the plugin
stays inactive.

### Configuration

Declare settings by subclassing `PluginSettings`. They are read from `UL_`-prefixed environment variables and `.env`,
the same way core's are:

```python
from urbanlens.dashboard.plugins.settings import PluginSettings


class StorefrontSettings(PluginSettings):
    api_key: str | None = None  # UL_STOREFRONT_API_KEY

    model_config = PluginSettings.config(prefix="UL_STOREFRONT_")


settings = StorefrontSettings.get()
```

To report misconfiguration through Django's system checks, override `check()`.

### Dependencies

A package plugin that needs Python packages core does not lists them in its own `requirements.txt`.
`bin/install_plugin_requirements.sh` installs every bundled plugin's requirements into the project environment.
The image build runs it, and so does CI. Run it yourself after `uv sync`, which removes packages that `uv.lock` does
not name. A distributed plugin declares its dependencies in its own package metadata instead.

## Data

A package plugin's models live in its own app. Its migrations are ordinary Django migrations, and
`manage.py migrate` creates its tables wherever it is installed. Core models never refer to plugin models. A plugin
that adds data to a core object keeps it in its own table, keyed to the core row:

```python
class StorefrontProfile(DashboardModel):
    user = OneToOneField(User, on_delete=CASCADE, related_name="+")
```

A plugin's foreign keys into core tables are enforced by the database with the `on_delete` they declare, so deleting a
core row still cascades into a disabled plugin's tables, which Django no longer knows about.

To show that data on a core page, use a template slot. To add it to core's API, contribute a `SerializerField` to
`points.SERIALIZER_FIELDS`: every external API serializer accepts plugin fields, by class or by a base of several.
A field reads its value from the object being rendered, so prefetch what it needs through
`points.API_LIST_QUERYSETS`, which also lets a plugin narrow a list endpoint by query parameters of its own.

### Lifecycle

| Action | Effect on tables |
| --- | --- |
| Add the plugin, run `migrate` | Its tables are created. |
| Disable it (`UL_DISABLED_PLUGINS`) | Untouched. Enabling it again carries on. |
| Remove its code | Untouched. Reinstalling it carries on. |
| `manage.py plugins uninstall NAME` (while it is installed) | Its migrations are reversed, dropping its tables and rows. |

To see each plugin's state and pending migrations, run `manage.py plugins`. To list every seam and who contributes
to it, run `manage.py plugins seams`.

### Taking over a table from an earlier release

If a release created a table under another app and a plugin now owns it, start the plugin's first migration with
`urbanlens.dashboard.plugins.migrations.AdoptModel` in place of `CreateModel`. Where the legacy table exists,
`AdoptModel` renames it and relabels its content type, so rows, permissions and generic relations carry over.
Elsewhere it creates the table.

Reversed by a rollback, it hands the table back under its legacy name, since the core migration reversed next expects
it. Reversed by `manage.py plugins uninstall`, it drops the table.

To move columns from a core table into a plugin's, copy them in a plugin data migration that declares `run_before` the
core migration removing them.

## Testing

`urbanlens.dashboard.plugins.testing` has three helpers:

- `contributed(name, seam)`: what one plugin added to a seam.
- `plugin_active(PluginClass)`: sets up a plugin for the duration of a block, then withdraws it.
- `plugin_withdrawn(name)`: shows core as an install without that plugin would see it. Routes are the exception,
  since the URLconf is built once; patch `reverse` where a test needs a plugin's routes gone.

Use `seam.overridden([...])` to make a seam hold exactly the given values for a block.

A package plugin's tests live in its `tests/` directory and run with the rest of the suite.
