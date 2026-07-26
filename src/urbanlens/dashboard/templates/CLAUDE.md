# dashboard/templates/ — Template & HTMX Gotchas

Applies to `src/urbanlens/dashboard/templates/`. 

- `htmx:afterSwap` fires before `showModal()` - initialize Leaflet maps inside dialogs from the
  after-request handler, not afterSwap.
- To make the page hero OOB-swappable, pass `id=` into the `_page_hero.html` include; never wrap it in a div.
- `_pagination_controls.html` assumes `request.path` is stable - for dual-rendered partials,
  hardcode `{% url %}` instead.
- Django's `page.next_page_number()` raises when exhausted and `|default:` does not catch it -
  branch the whole tag on `has_next`.
- The `add` filter silently returns `''` for `"prefix-"|add:obj.id` (str + int fails both its
  int-coercion and concat attempts). Always `|stringformat:"s"` the id first - an empty result
  collapses per-item DOM ids into duplicates (e.g. every DM map bubble rendered the first map).
- Do not put `{# -- #}` multi-line comments in template files. They will be displayed directly to
  the user on the frontend.
