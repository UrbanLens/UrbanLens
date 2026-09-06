# N7 — Two Django template traps that each shipped a 500

`id: N7` · `status: current` · `updated: 2026-09-06`

Both found on 2026-09-06, both while chasing something else, both live on
`release/v_0_8_0` until that day. Neither is exotic; both are shapes this codebase
will write again.

## A filter *argument* has no failure tolerance

```django
{{ pin.slug|default:wiki.location.slug|default:"" }}
```

Django's `FilterExpression.resolve` catches `VariableDoesNotExist` for the **primary**
variable and substitutes `string_if_invalid` — but it resolves each filter *argument*
with a plain `Variable.resolve`, and that raises. So naming a variable the current page
does not have is a silent no-op in the first position and a **500** in the second.

`_page_hero.html` is shared between the Private Pin page and the wiki page, so by
construction only one of `pin`/`wiki` is ever in context. The expression above sits
inside `{% if hero_image_url %}`, and the pin page is the only include site that passes
`hero_image_url` — so **every pin that had a cover photo returned a 500**. That guard is
also why nobody noticed: no test gave a pin a cover photo, and neither probe pin on the
dev stack had one.

Confirmed in a browser against the running dev stack, not only in a test: 500 before,
200 after.

**Use `{% firstof a b "" %}`.** `FirstOfNode` resolves each name with
`ignore_failures=True`, which is what a "whichever of these exists" expression wants.

Worth noting the second-order effect: because the *primary* variable failing skips the
filters with it, the same line would have silently dropped the slug on any page that
had `wiki` and not `pin` — so the wiki cover position would have been saved under one
shared key for every wiki. That half never shipped, only because the wiki page has its
own cover hero.

## `.image.url` raises when the row has no stored file

`Image.display_url` exists for this and says so in its own docstring. Sixteen template
reads across eleven files went to `image.url` anyway, so **one** photo row whose file
never landed — a failed external download keeps its `source_url` and nothing else — was
a 500 for the whole panel rather than one missing thumbnail.

Ten of the eleven, to be exact: `_widget_recent_photos.html` reads through a queryset
that already calls `.with_file()`, so that one could not actually have raised. Its read
was replaced anyway — the guarantee lived in a controller two files away, and nothing
said so at the read.

`bin/check_image_file_reads.py` now guards it, and accepts either shape: a fall-back
property (`display_url`/`thumb_url`), or a `{% if x.image %}` (or `.image.name`) on a
block **enclosing** the read. The second is what the comment attachments already do and
they are correct — those are `Comment`'s own `ImageField`, not an `Image`, and have no
`display_url` to reach for.

Enclosing, not merely earlier: the first version matched "anywhere above", which let an
earlier `{% for photo in captions %}{% if photo.image %}` vouch for a later
`{% for photo in something_else %}` that had no guard at all. A lint that can be
satisfied by an unrelated block is worse than none, because it reads as coverage.

## Proposed wording for `templates/CLAUDE.md`

Jess: these belong in that file's gotcha list rather than here, if you agree with them.
Suggested text, to sit under the existing `add`-filter bullet:

> - A filter *argument* is resolved with no failure tolerance, unlike the value being
>   filtered. `{{ a|default:b.c }}` raises `VariableDoesNotExist` out of the render when
>   `b` is not in context — a 500, where the same name in the first position would have
>   been silently blank. Use `{% firstof a b.c "" %}` for "whichever of these exists".
> - `{{ img.image.url }}` raises when the row has no stored file. Read `display_url` (or
>   `thumb_url`) instead, or guard with `{% if img.image %}`.
>   `bin/check_image_file_reads.py` enforces it.
