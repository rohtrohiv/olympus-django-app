from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """
    Template filter to get item from dictionary by key.
    Usage: {{ my_dict|get_item:"key_name" }}
    """
    if dictionary is None:
        return None
    return dictionary.get(key)

@register.filter
def mul(value, arg):
    """
    Multiply the value by the arg.
    Usage: {{ value|mul:3 }}
    """
    try:
        return int(value) * int(arg)
    except (ValueError, TypeError):
        return ''

@register.filter(name='add')
def add_filter(value, arg):
    """
    Add the arg to the value.
    Usage: {{ value|add:3 }}
    """
    try:
        return int(value) + int(arg)
    except (ValueError, TypeError):
        try:
            return value + arg
        except:
            return ''
