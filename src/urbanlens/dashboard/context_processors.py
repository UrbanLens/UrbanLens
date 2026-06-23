import re


def add_site_settings(request):
    """Inject site-wide settings into every template context.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with site_title available in all templates.
    """
    try:
        from urbanlens.dashboard.models.site_settings import SiteSettings
        site = SiteSettings.get_current()
        return {"site_title": site.app_title}
    except Exception:
        return {"site_title": "UrbanLens"}


def add_page_name(request):
    resolver_match = request.resolver_match
    if resolver_match is None:
        return {"page_name": ""}
    page_name = resolver_match.url_name or ""
    # This will be a className, so replace anything that would trip up css
    page_name = re.sub(r"[^a-zA-Z0-9]", "-", page_name)
    return {"page_name": page_name}
