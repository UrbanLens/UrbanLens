"""Template slots: places in core pages where plugins render fragments, with the page's context."""

from __future__ import annotations

from urbanlens.dashboard.plugins.extensions import TemplateSlot

SLOT_HEAD = TemplateSlot("page.head", "Inside <head> on every page.")
SLOT_BODY_END = TemplateSlot("page.body_end", "Just before </body> on every page: dialogs, overlays.")
SLOT_ACCOUNT_MENU = TemplateSlot("page.account_menu", 'Items in the signed-in account menu, as ``<a class="nav-dropdown-item" role="menuitem">`` links.')
SLOT_FOOTER_LINKS = TemplateSlot("page.footer_links", "Links in the page footer, each preceded by `` &middot; ``.")
SLOT_SETTINGS_SECTIONS = TemplateSlot("settings.sections", "Sections at the end of the user settings page.")
SLOT_SETTINGS_TABS = TemplateSlot("settings.tabs", "Tab buttons on the user settings page, after core's own; render ``settings-tab`` buttons with ``data-tab`` matching a panel.")
SLOT_SETTINGS_TAB_PANELS = TemplateSlot("settings.tab_panels", "Tab panels on the user settings page, after core's own; each is a ``settings-tab-panel`` with a ``data-tab-panel`` attribute.")
SLOT_SITE_ADMIN_TABS = TemplateSlot("site_admin.tabs", "Tabs in the site-admin navigation.")
SLOT_SITE_ADMIN_HOME_CARDS = TemplateSlot("site_admin.home.cards", "Cards on the site-admin home page.")
SLOT_SITE_ADMIN_API_LIMITS = TemplateSlot("site_admin.api_limits", "Cards at the end of the site-admin API-limits page, after the service tabs.")
SLOT_SITE_ADMIN_ROLES = TemplateSlot("site_admin.roles", "Cards on the site-admin roles page, after the grants; ``roles`` is in context.")
SLOT_FEATURE_LOCKED = TemplateSlot(
    "features.locked",
    "After a notice that the viewer lacks a site feature, such as how to get it. ``feature`` (a SiteFeature value) is in context.",
)
SLOT_LABEL_DIALOG_SUGGESTIONS = TemplateSlot("labels.dialog_suggestions", 'Above the label list in the Add Labels dialog; ``membership_route`` ("pin", "location"...) and ``obj_uuid`` (the pin or location slug) are in context.')
SLOT_PIN_PANELS = TemplateSlot("pin.panels", "Panels on the private pin page after core's own; ``pin`` is in context.")
SLOT_WIKI_PANELS = TemplateSlot("wiki.panels", "Panels on the wiki page after core's own; ``wiki`` is in context.")
SLOT_FAQ_IMPORT_TIPS = TemplateSlot("faq.import_tips", "List items in the FAQ's answer about importing, each an ``<li>``.")
SLOT_SETUP_HIGHLIGHTS = TemplateSlot("setup.highlights", "Highlights on the setup wizard's welcome step, after core's own; render the same ``setup-highlights__item`` markup.")
SLOT_LINK_ACTIONS = TemplateSlot("link.actions", "Controls beside an external link on a pin, wiki or search result; ``url`` is the link, and the page context is in scope.")
SLOT_SETUP_FEATURES = TemplateSlot("setup.features", "Feature groups on the setup wizard's integrations step, after core's own; render the same ``setup-feature-group`` markup.")
SLOT_HELP_IMPORT_PINS_NAV = TemplateSlot("help.import_pins.nav", "Links in the import help page's contents list, after core's own; each is an ``<a>``.")
SLOT_HELP_IMPORT_PINS_SECTIONS = TemplateSlot("help.import_pins.sections", "Sections on the import help page after core's own, before the supported formats.")
SLOT_HELP_IMPORT_FORMATS = TemplateSlot("help.import_formats", "Format labels in the import help page's supported-formats grid, each a ``<span>``.")
SLOT_IMPORT_PINS_HELP = TemplateSlot("import.pins.help", "Notes on the location import page after its file-type explanation; ``profile`` is in context.")
SLOT_THANKS_ATTRIBUTIONS = TemplateSlot("thanks.attributions", "Entries on the thanks page's attribution list after core's own, each an ``attr-list-item``.")
SLOT_TRIP_DETAIL_PANELS = TemplateSlot("trip.detail.panels", "Panels on the trip page after core's own; ``trip`` is in context.")

__all__ = [
    "SLOT_ACCOUNT_MENU",
    "SLOT_BODY_END",
    "SLOT_FAQ_IMPORT_TIPS",
    "SLOT_FEATURE_LOCKED",
    "SLOT_FOOTER_LINKS",
    "SLOT_HEAD",
    "SLOT_HELP_IMPORT_FORMATS",
    "SLOT_HELP_IMPORT_PINS_NAV",
    "SLOT_HELP_IMPORT_PINS_SECTIONS",
    "SLOT_IMPORT_PINS_HELP",
    "SLOT_LABEL_DIALOG_SUGGESTIONS",
    "SLOT_LINK_ACTIONS",
    "SLOT_PIN_PANELS",
    "SLOT_SETTINGS_SECTIONS",
    "SLOT_SETTINGS_TABS",
    "SLOT_SETTINGS_TAB_PANELS",
    "SLOT_SETUP_FEATURES",
    "SLOT_SETUP_HIGHLIGHTS",
    "SLOT_SITE_ADMIN_API_LIMITS",
    "SLOT_SITE_ADMIN_HOME_CARDS",
    "SLOT_SITE_ADMIN_ROLES",
    "SLOT_SITE_ADMIN_TABS",
    "SLOT_THANKS_ATTRIBUTIONS",
    "SLOT_TRIP_DETAIL_PANELS",
    "SLOT_WIKI_PANELS",
]
