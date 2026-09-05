"""
Every field says what it is, where the browser will repeat it back.

Reported as: *the tooltip on most fields just says "Please fill out this
field" instead of something useful.* It does, and that message is the
browser's — it is what a control with a `required` attribute and no
description of its own has to offer. The screen around the box may explain it
perfectly; the bubble the browser puts over the box cannot see any of that.

So each field carries a sentence saying what it is or does, in `title`. Three
things read it and none of them needed a new mechanism:

* the browser, as a hover tooltip on a desktop;
* `forms.js`, which uses it in place of *"Please fill out this field"* when a
  required one is left empty;
* a screen reader, as the field's description.

`title` rather than `help_text` on purpose. Help text is printed under the box
on every form that renders it, and a paragraph under all one hundred and
ninety fields in this application would bury the handful that are genuinely
surprising — a description is worth reading when you go looking for it, and
noise when it arrives unasked. Where a field already has help text it keeps it
and the same words become the tooltip, so nothing is said twice in two ways.

Swapped in at form level rather than attribute by attribute, for the reason
`MoneyFormMixin` gives: the next field added to a form should be described
because somebody wrote a line in a dict beside it, not because they remembered
this file exists. `tests_forms.py` fails the build when one is not.
"""

from __future__ import annotations


class DescribedFields:
    """Mix in first, ahead of any mixin that replaces fields.

    `MoneyFormMixin` swaps `*_minor` for a `MoneyFormField`, and a title
    written onto the field it replaced would go with it. Listing this first
    means its work happens after every `__init__` below it has run.

    A form that adds fields in its *own* `__init__` — `FirstRunForm` builds
    the shop questions from the settings registry — calls `describe_fields()`
    again at the end of it. The pass only fills in what is missing, so running
    it twice describes the late arrivals and disturbs nothing.
    """

    #: `{field name: what it is or does}`, one sentence, in the operator's
    #: words rather than the column's. A field with help text of its own needs
    #: no entry — that text is already the answer.
    descriptions: dict = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.describe_fields()

    def describe_fields(self) -> None:
        from django import forms

        for name, field in self.fields.items():
            # A hidden input has nothing to hover and never raises a bubble.
            # Describing one would only be a string nobody can reach.
            if isinstance(field.widget, forms.HiddenInput):
                continue
            said = self.descriptions.get(name) or field.help_text
            if said:
                # `setdefault`: a widget that was handed a title by the form
                # that built it knows something this dict does not.
                field.widget.attrs.setdefault("title", said)
