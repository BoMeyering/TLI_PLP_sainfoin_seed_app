from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    """`{{ some_dict|get_item:key }}` — dict lookup with a variable key."""
    try:
        return mapping.get(key)
    except AttributeError:
        return None
