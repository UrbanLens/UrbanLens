from django import forms

from urbanlens.dashboard.models.reviews.model import Review


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ["rating", "review"]
