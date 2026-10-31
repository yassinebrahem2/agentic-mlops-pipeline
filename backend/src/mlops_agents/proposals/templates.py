"""The template grammar of the stage 3 overview, one rule for the validator and for any filler.

A command template or a ci_dispatch input value is a Python format string whose replacement
fields are bare declared names, ``{name}``: no format spec, no conversion, no nested field. A
literal brace is written ``{{`` or ``}}``. A filler parses with the same ``string.Formatter``
and fills with ``str.format_map``, never a plain text replace.
"""

from collections.abc import Collection, Mapping
from string import Formatter

_BRACE_HINT = "a literal brace is written {{ or }}"


def _shown(name: str, spec: str, conversion: str | None) -> str:
    return (
        "{" + name + (f"!{conversion}" if conversion else "") + (f":{spec}" if spec else "") + "}"
    )


def check_template(where: str, template: str, declared: Collection[str]) -> None:
    """Raise ValueError, naming ``where`` and the field, unless the template follows the grammar."""
    try:
        # parse is lazy: a stray brace raises while it is iterated.
        fields = [
            (name, spec or "", conversion)
            for _, name, spec, conversion in Formatter().parse(template)
            if name is not None
        ]
    except ValueError as err:
        raise ValueError(f"{where} does not parse as a template ({err}); {_BRACE_HINT}") from err
    for name, spec, conversion in fields:
        if spec or conversion is not None:
            raise ValueError(
                f"{where} has the field {_shown(name, spec, conversion)}: only a bare "
                f"{{name}} is allowed, with no format spec or conversion; {_BRACE_HINT}"
            )
        if name not in declared:
            raise ValueError(
                f"{where} uses {{{name}}}, which the command does not declare as a "
                f"placeholder; {_BRACE_HINT}"
            )


def fill_template(template: str, values: Mapping[str, str]) -> str:
    """Parse template with string.Formatter, check all placeholders have values in values,
    and fill with str.format_map."""
    from mlops_agents.monitoring.hooks.contract import HookRefused, MissingPlaceholder

    try:
        fields = [name for _, name, _, _ in Formatter().parse(template) if name is not None]
    except ValueError as err:
        raise HookRefused(f"template does not parse as a template ({err}); {_BRACE_HINT}") from err

    for name in fields:
        if name not in values:
            raise MissingPlaceholder(name)

    return template.format_map(values)
