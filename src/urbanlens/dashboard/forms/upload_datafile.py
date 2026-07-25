from django import forms

from urbanlens.dashboard.services.ai.document_import import SUPPORTED_DOCUMENT_EXTENSIONS
from urbanlens.dashboard.services.archive_extractor import _ARCHIVE_ALLOWED_EXTENSIONS

# Mirrors the 500 MB cap already enforced downstream for the analogous export/import
# ZIP upload (see controllers.tools._MAX_IMPORT_SIZE_BYTES) - rejecting an oversized
# file here, at the form layer, avoids ever handing it to archive extraction.
_MAX_UPLOAD_FILE_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB

# Extensions accepted for a single uploaded file: outer archive containers
# (extract_archive/is_archive only actually recognise ZIP and GZIP/TGZ magic bytes -
# a bare ".tar" without gzip compression is not supported), the individual location-data
# formats archive_extractor knows how to pull out of - or accept directly outside of -
# an archive, and the plain-text/Word documents routed to the AI import pipeline.
_ARCHIVE_CONTAINER_EXTENSIONS = frozenset({"zip", "tgz", "gz"})
_ALLOWED_UPLOAD_EXTENSIONS = _ARCHIVE_CONTAINER_EXTENSIONS | _ARCHIVE_ALLOWED_EXTENSIONS | SUPPORTED_DOCUMENT_EXTENSIONS


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
            ValidationError: When no files are provided and the field is required,
                or when a file exceeds the size ceiling or has a disallowed extension.
        """
        if not data:
            raise forms.ValidationError(self.error_messages["required"], code="required")
        single_clean = super().clean
        cleaned_files = []
        for f in data:
            if not f:
                continue
            cleaned_file = single_clean(f, initial)
            self._validate_upload(cleaned_file)
            cleaned_files.append(cleaned_file)
        return cleaned_files

    def _validate_upload(self, uploaded_file) -> None:
        """Reject an uploaded file that is too large or has an unsupported extension.

        This is a defense-in-depth check at the form layer - the consuming view
        feeds these bytes into archive detection/extraction, which should never
        see an oversized or clearly-unsupported file in the first place.

        Args:
            uploaded_file: The cleaned uploaded-file object to validate.

        Raises:
            ValidationError: When the file exceeds ``_MAX_UPLOAD_FILE_SIZE_BYTES``
                or its extension is not in ``_ALLOWED_UPLOAD_EXTENSIONS``.
        """
        name = uploaded_file.name or ""
        size = getattr(uploaded_file, "size", None)
        if size is not None and size > _MAX_UPLOAD_FILE_SIZE_BYTES:
            raise forms.ValidationError(
                f'"{name}" is too large (max {_MAX_UPLOAD_FILE_SIZE_BYTES // (1024 * 1024)} MB).',
                code="file_too_large",
            )

        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if extension not in _ALLOWED_UPLOAD_EXTENSIONS:
            raise forms.ValidationError(
                f'"{name}" has an unsupported file type.',
                code="unsupported_extension",
            )


class UploadDataFile(forms.Form):
    """Form for uploading one or more location data files."""

    upload_files = _MultipleFileField(label="Files")
