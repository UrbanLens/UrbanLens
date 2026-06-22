import re


def add_page_name(request):
    resolver_match = request.resolver_match
    if resolver_match is None:
        return {"page_name": ""}
    page_name = resolver_match.url_name or ""
    # This will be a className, so replace anything that would trip up css
    page_name = re.sub(r"[^a-zA-Z0-9]", "-", page_name)
    return {"page_name": page_name}
