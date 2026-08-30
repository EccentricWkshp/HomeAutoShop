"""People views (SPEC §7.2)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from .models import Person


class PersonForm(forms.ModelForm):
    class Meta:
        model = Person
        fields = ["display_name", "given_name", "family_name", "email", "phone", "address", "notes", "is_household"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2}), "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name == "display_name"
            if not isinstance(field.widget, forms.CheckboxInput):
                css = "input textarea" if isinstance(field.widget, forms.Textarea) else "input"
                field.widget.attrs.setdefault("class", css)


@login_required
def person_list(request):
    return render(request, "people/list.html", {"people": Person.objects.all()})


@login_required
def person_detail(request, pk):
    person = get_object_or_404(Person, pk=pk)
    return render(
        request,
        "people/detail.html",
        {
            "person": person,
            "current_assets": person.current_assets(),
            "former_assets": person.former_assets(),
        },
    )


@login_required
def person_create(request):
    if request.method == "POST":
        form = PersonForm(request.POST)
        if form.is_valid():
            person = form.save()
            messages.success(request, _("Added %(name)s.") % {"name": person.display_name})
            return redirect("person_detail", pk=person.pk)
    else:
        form = PersonForm()
    return render(request, "people/form.html", {"form": form, "person": None})


@login_required
def person_edit(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if request.method == "POST":
        form = PersonForm(request.POST, instance=person)
        if form.is_valid():
            form.save()
            messages.success(request, _("Saved."))
            return redirect("person_detail", pk=person.pk)
    else:
        form = PersonForm(instance=person)
    return render(request, "people/form.html", {"form": form, "person": person})
