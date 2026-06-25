from django import forms


class _MultipleFileInput(forms.ClearableFileInput):
    """File input widget that accepts multiple files via ``<input multiple>``."""

    allow_multiple_selected = True

    def value_from_datadict(self, data, files, name):
        return files.getlist(name)


class _MultipleFileField(forms.FileField):
    """FileField variant that returns a list of uploaded files.

    Wraps Django's FileField so each file in a multi-file upload is validated
    individually while the field as a whole returns a list.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", _MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        """Validate each uploaded file and return a list.

        Args:
            data: List of uploaded file objects from the widget.
            initial: Initial value (unused for file uploads).

        Returns:
            List of validated uploaded-file objects.

        Raises:
            ValidationError: When no files are provided and the field is required.
        """
        if not data:
            raise forms.ValidationError(self.error_messages["required"], code="required")
        single_clean = super().clean
        return [single_clean(f, initial) for f in data if f]


class UploadDataFile(forms.Form):
    """Form for uploading one or more location data files."""

    upload_files = _MultipleFileField(label="Files")
