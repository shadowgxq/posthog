from rest_framework import serializers


def _validate_folder_path(value: str) -> str:
    from posthog.api.file_system.file_system import validate_file_system_path

    return validate_file_system_path(value)


def move_to_folder_field(noun: str) -> serializers.CharField:
    return serializers.CharField(
        required=False,
        write_only=True,
        validators=[_validate_folder_path],
        help_text=(
            f"Project-tree folder to move this {noun} into, e.g. 'Experiments' or 'Unfiled/Dashboards/Growth'. "
            f"Separate nested folders with '/'; folders that do not exist yet are created. "
            f"The {noun} keeps its own name. "
            f"Has no effect when the {noun} has no file system entry."
        ),
    )
