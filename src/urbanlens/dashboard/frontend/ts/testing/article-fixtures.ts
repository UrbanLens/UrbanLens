/**
 * Saved article Markdown of the kinds the editor meets, for round-trip tests.
 */

/** Shaped exactly as `services/wiki/wiki_seed.py` writes a Wikipedia-seeded article (no trailing newline). */
export const SEEDED_ARTICLE = `![Brookfield Mill](/media/remote/3f9c2a/brookfield-mill.jpg)

- **Location:** Brookfield, Massachusetts
- **Built:** 1872
- **Architect:** *Unknown*
- **Status:** Abandoned

**Brookfield Mill** is a former textile mill in *Brookfield, Massachusetts*. It operated from 1872 until 1958.
It was listed on the National Register in 1983.

## History

The mill was built by the **Quaboag Manufacturing Company** after the 1871 flood destroyed the earlier dam.

1. Dam rebuilt in 1872
2. Weave shed added in 1890
3. Closed in 1958

## Architecture

**Weave shed**
: A single-storey sawtooth-roofed hall.
**Boiler house**
: Brick, with a 40 m chimney.

> The mill was the largest employer in the town for eighty years.

---

*This article was started from [Wikipedia](https://en.wikipedia.org/wiki/Brookfield_Mill) (Brookfield Mill), licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Feel free to expand and edit it.*`;

/** Written by hand in Source mode, using most of what the server renderer supports. */
export const HAND_WRITTEN_ARTICLE = `# Hollow Creek Asylum

Setext Heading
==============

Sub heading
-----------

The east wing was _sealed_ in 2009 and __re-opened__ for demolition surveys; see the [county record][county] and the [state survey](https://example.org/survey "State survey 2014").
Hard-wrapped lines continue
the same paragraph.

* Access via the north fence
* Bring a respirator
    * nested with four spaces
    * second nested
* Done

1) First
2) Second

3. Loose item one

4. Loose item two

| Floor | Condition | Notes |
|:------|:---------:|------:|
| 1     | Fair      | Water damage |
| 2     | *Poor*    | Collapsed \`stairs\` |

![East wing][east]

![Boiler](https://img.example/boiler.jpg "Boiler room, 2019")

<img src="https://img.example/raw.png" width="300">

<div class="note">
**Warning:** asbestos throughout.
</div>

Press <kbd>Ctrl</kbd>+<kbd>F</kbd> to search the logbook. Water rose to the 2<sup>nd</sup> floor.

The asylum opened in 1889.[^1] It closed in 1994.[^note] Demolition began in 2016.[^3]

Inline note^[Unverified, from a forum post.] here.

\`\`\`python
def visit(site):
    return site.open  # *not* emphasis
\`\`\`

~~~
tilde fence
~~~

    indented code block

Escapes: \\*not italic\\*, 5 \\* 3, \\_underscores\\_, \\# not heading, a\\\\b, \\[not a link\\].

Snake_case_word and 2*3*4 and **bold**text and ***both***.

Entities: &copy; 2020 &amp; &lt;tag&gt; &nbsp;space.

Line one with backslash\\
line two.

Autolink <https://example.com/a?b=1&c=2> and bare https://example.org/page and email <mail@example.com>.

> Quote with a list:
> - one
> - two
>
> > nested quote

***

<!-- editor note: verify dates -->

Final paragraph ending with trailing spaces.

[county]: https://county.example/records/123 "County records"
[east]: https://img.example/east.jpg

[^1]: https://archive.example/opening-1889
[^note]: Smith, J. *Asylums of New England* (2004), p. 12.
    Continued on the next line.
[^3]: First paragraph of the note.

    Second paragraph, indented under the note.
`;

/** What the WYSIWYG canvas itself writes. */
export const WYSIWYG_ARTICLE = `## Getting in

The **north fence** has a gap; the *south gate* is alarmed. See [the map](https://example.org/map "Site map").

- Bring a respirator
- Watch the \`stairs\`

3. Third
4. Fourth

> Leave only footprints.

\`\`\`text
GPS 42.1, -71.2
\`\`\`

---

![Boiler room](/media/image/4f3a/)

| Floor | Condition |
| --- | --- |
| 1 | Fair |

First line\\
second line

### Sources

Built in 1902.[^1]

[^1]: County archive, box 12.
`;

export const ARTICLE_FIXTURES: Record<string, string> = {
    seeded: SEEDED_ARTICLE,
    "hand-written": HAND_WRITTEN_ARTICLE,
    wysiwyg: WYSIWYG_ARTICLE,
};
